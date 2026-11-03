from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import User
from .models import ClaimRequest
from .telegram_totals import daily_approval_breakdown, telegram_message_parts
from .views import notify


class TelegramDailyBreakdownTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username='breakdown-requester')
        self.supervisor = User.objects.create_user(username='breakdown-supervisor')
        self.manager = User.objects.create_user(username='breakdown-manager')
        self.cfo = User.objects.create_user(username='breakdown-cfo')

    def claim(self, document, requested, actual=None, **kwargs):
        fields = dict(requester=self.requester, supervisor=self.supervisor,
            accounting_manager=self.manager, cfo=self.cfo, request_date=date(2026, 10, 30),
            document_number=document, status=ClaimRequest.Status.PENDING_ACCOUNTING,
            requested_total=Decimal(requested), actual_total=Decimal(actual) if actual is not None else None,
            approval_threshold=Decimal('5000'), purpose='Daily breakdown', work_location='HQ')
        fields.update(kwargs)
        return ClaimRequest.objects.create(**fields)

    def message(self, claim, recipient=None):
        with patch('apps.claims.views.send_telegram_notification') as bot, patch('apps.claims.views.send_telegram_group_notification') as group:
            notify(recipient or self.manager, 'รออนุมัติ', 'กรุณาตรวจคำขอ', claim)
        texts = [call.args[2] for call in bot.call_args_list]
        self.assertEqual(texts, [call.args[1] for call in group.call_args_list])
        return ''.join(texts)

    def test_second_or_third_claim_shows_every_previous_amount_and_own_without_double_count(self):
        self.claim('BR-PREVIOUS-01', '3000')
        self.claim('BR-PREVIOUS-02', '1500')
        current = self.claim('BR-CURRENT-03', '1000', daily_requested_total=Decimal('5500'))
        for recipient in (self.supervisor, self.manager, self.cfo):
            text = self.message(current, recipient)
            self.assertIn('BR-PREVIOUS-01: 3,000.00 บาท', text)
            self.assertIn('BR-PREVIOUS-02: 1,500.00 บาท', text)
            self.assertIn('ใบนี้ BR-CURRENT-03: 1,000.00 บาท', text)
            self.assertIn('รวมตามรายละเอียดล่าสุด: 5,500.00 บาท', text)
            self.assertIn('ต้องให้ CFO อนุมัติ', text)

    def test_completed_claim_uses_80_not_requested_100(self):
        self.claim('BR-CLOSED', '100', '80', status=ClaimRequest.Status.COMPLETED)
        current = self.claim('BR-CURRENT', '4950', daily_requested_total=Decimal('5030'))
        text = self.message(current)
        self.assertIn('BR-CLOSED: 80.00 บาท (ใช้จริง ปิดแล้ว)', text)
        self.assertIn('รวมตามรายละเอียดล่าสุด: 5,030.00 บาท', text)
        self.assertNotIn('BR-CLOSED: 100.00', text)

    def test_same_person_day_status_scope_keeps_cross_day_requirement_deferred(self):
        self.claim('BR-VALID', '3000')
        for status in ('DRAFT', 'RETURNED', 'REJECTED', 'CANCELLED'):
            self.claim(f'EXCLUDED-{status}', '9000', status=status)
        self.claim('EXCLUDED-DAY', '9000', request_date=date(2026, 10, 29))
        other = User.objects.create_user(username='breakdown-other')
        self.claim('EXCLUDED-PERSON', '9000', requester=other)
        current = self.claim('BR-CURRENT', '2010', daily_requested_total=Decimal('5010'))
        text = self.message(current)
        self.assertIn('BR-VALID: 3,000.00 บาท', text)
        self.assertNotIn('EXCLUDED-', text)
        self.assertIn('รวมตามรายละเอียดล่าสุด: 5,010.00 บาท', text)

    def test_zero_legacy_unknown_and_known_pending_excess_follow_existing_daily_math(self):
        self.claim('BR-ZERO', '1000', '0', status=ClaimRequest.Status.COMPLETED)
        self.claim('BR-LEGACY', '100', status=ClaimRequest.Status.COMPLETED)
        self.claim('BR-EXCESS', '100', '150', status=ClaimRequest.Status.PENDING_EXCESS_APPROVAL)
        current = self.claim('BR-CURRENT', '5000', daily_requested_total=Decimal('5250'))
        text = self.message(current)
        self.assertIn('BR-ZERO: 0.00 บาท', text)
        self.assertIn('BR-LEGACY: 100.00 บาท', text)
        self.assertIn('BR-EXCESS: 150.00 บาท (รวมยอดใช้เกิน)', text)
        self.assertIn('รวมตามรายละเอียดล่าสุด: 5,250.00 บาท', text)

    def test_later_actual_change_is_labeled_not_falsely_presented_as_original_snapshot(self):
        previous = self.claim('BR-CLOSED', '100', '80', status=ClaimRequest.Status.COMPLETED)
        current = self.claim('BR-CURRENT', '4950', daily_requested_total=Decimal('5030'))
        previous.actual_total = Decimal('70')
        previous.save(update_fields=['actual_total'])
        text = self.message(current)
        self.assertIn('BR-CLOSED: 70.00 บาท', text)
        self.assertIn('รวมตามรายละเอียดล่าสุด: 5,020.00 บาท', text)
        self.assertIn('ใช้อนุมัติรอบนี้ยังเป็น 5,030.00 บาท ไม่ได้เปลี่ยนย้อนหลัง', text)
        current.refresh_from_db()
        self.assertEqual(current.daily_requested_total, Decimal('5030'))

    def test_excess_and_waiting_payment_use_actual_total_not_add_supplemental_twice(self):
        self.claim('BR-PREVIOUS', '1000')
        current = self.claim('BR-CURRENT', '4000', '5500', status=ClaimRequest.Status.PENDING_EXCESS_APPROVAL,
            daily_requested_total=Decimal('5000'), excess_daily_total=Decimal('6500'))
        for status in (ClaimRequest.Status.PENDING_EXCESS_APPROVAL, ClaimRequest.Status.PENDING_PAYMENT):
            current.status = status
            text = self.message(current)
            self.assertIn('ใบนี้ BR-CURRENT: 5,500.00 บาท (ยอดใช้จริงรวม)', text)
            self.assertIn('รวมตามรายละเอียดล่าสุด: 6,500.00 บาท', text)
            self.assertNotIn('ใบนี้ BR-CURRENT: 1,500.00', text)

    def test_claim_submitted_after_this_round_is_not_added_to_earlier_approval_details(self):
        self.claim('BR-PREVIOUS', '3000')
        current = self.claim('BR-CURRENT', '2010', daily_requested_total=Decimal('5010'))
        current.approval_records.create(actor=self.requester, action='SUBMITTED', status_before='DRAFT', status_after='PENDING_SUPERVISOR')
        later = self.claim('BR-LATER', '9000')
        later.approval_records.create(actor=self.requester, action='SUBMITTED', status_before='DRAFT', status_after='PENDING_SUPERVISOR')
        text = self.message(current)
        self.assertNotIn('BR-LATER:', text)
        self.assertIn('รวมตามรายละเอียดล่าสุด: 5,010.00 บาท', text)

    def test_no_breakdown_for_budget_owner_receipt_completion_or_exactly_5000(self):
        self.claim('BR-PREVIOUS', '3000')
        current = self.claim('BR-CURRENT', '2000', daily_requested_total=Decimal('5000'))
        self.assertNotIn('รายละเอียดใบก่อน', self.message(current))
        current.daily_requested_total = Decimal('5100')
        for status in (ClaimRequest.Status.PENDING_BUDGET_APPROVAL, ClaimRequest.Status.PAID, ClaimRequest.Status.COMPLETED):
            current.status = status
            self.assertNotIn('รายละเอียดใบก่อน', self.message(current))

    def test_missing_previous_evidence_is_explained_not_invented(self):
        current = self.claim('BR-CURRENT', '100', daily_requested_total=Decimal('5100'))
        text = self.message(current)
        self.assertIn('ไม่พบใบก่อนที่นับได้ตามข้อมูลปัจจุบัน', text)
        self.assertIn('ใช้อนุมัติรอบนี้ยังเป็น 5,100.00 บาท', text)
        self.assertNotIn('ใบก่อน: 5,000.00', text)

    def test_long_breakdown_is_sent_in_order_without_losing_rows_buttons_only_on_last_part(self):
        for index in range(70):
            self.claim(f'BR-PREVIOUS-{index:03d}', '100')
        current = self.claim('BR-CURRENT', '1000', daily_requested_total=Decimal('8000'))
        with patch('apps.claims.views.send_telegram_notification') as bot, patch('apps.claims.views.send_telegram_group_notification') as group:
            notify(self.manager, 'รออนุมัติ', 'กรุณาตรวจคำขอ', current)
        self.assertGreater(bot.call_count, 1)
        self.assertEqual(bot.call_count, group.call_count)
        joined = ''.join(call.args[2] for call in bot.call_args_list)
        for index in range(70):
            self.assertEqual(joined.count(f'BR-PREVIOUS-{index:03d}:'), 1)
        self.assertIn('รวมตามรายละเอียดล่าสุด: 8,000.00 บาท', joined)
        for direct, grouped in zip(bot.call_args_list, group.call_args_list):
            self.assertEqual(direct.args[2], grouped.args[1])
            self.assertLessEqual(len(direct.args[2]), 2800)
        self.assertTrue(all(call.args[4] is None for call in bot.call_args_list[:-1]))
        self.assertIsNotNone(bot.call_args_list[-1].args[4])

    def test_splitter_preserves_all_text_including_a_very_long_line(self):
        message = 'รายการ\n' + 'ก' * 6000 + '\nท้ายรายการ'
        parts = telegram_message_parts(message)
        self.assertEqual(''.join(parts), message)
        self.assertTrue(all(len(part) <= 2800 for part in parts))
