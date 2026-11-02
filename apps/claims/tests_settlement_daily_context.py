from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import User
from .models import ClaimRequest
from .views import notify


class SettlementDailyContextTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username='settlement-day-requester')
        self.claim = ClaimRequest.objects.create(requester=self.requester, request_date=date(2026, 10, 24),
            status=ClaimRequest.Status.PENDING_PAYMENT, request_type='ADVANCE',
            purpose='Supplemental payment', work_location='HQ', requested_total='4000',
            actual_total='5500', daily_requested_total='4100', excess_daily_total='5580')
        self.claim.refresh_from_db()

    def message(self, **kwargs):
        with patch('apps.claims.views.send_telegram_notification') as bot, patch('apps.claims.views.send_telegram_group_notification') as group:
            notify(self.requester, 'Status update', 'Review', self.claim, **kwargs)
        result = bot.call_args.args[2]
        self.assertEqual(result, group.call_args.args[1])
        return result

    def test_waiting_payment_shows_excess_snapshot_not_requested_total_or_live_actual_sum(self):
        text = self.message()
        self.assertIn('ยอดสะสมสำหรับอนุมัติ รวมคำขอนี้: 5,580.00 บาท', text)
        self.assertNotIn('4,100.00', text)
        self.assertNotIn('ยอดใช้จริงสะสม', text)
        self.assertNotIn('ต้องให้ CFO อนุมัติ', text)
        self.claim.actual_total = None
        self.assertIn('รวมคำขอนี้: 4,100.00 บาท', self.message())

    def test_paid_awaiting_receipt_has_no_daily_context(self):
        self.claim.status = ClaimRequest.Status.PAID
        text = self.message()
        self.assertNotIn('ยอดสะสม', text)
        self.assertNotIn('ยอดใช้จริงสะสม', text)
        self.assertIn('ยอดใช้จริงรวม: 5,500.00 บาท', text)

    def test_completed_actual_daily_sum_matches_web_rules_and_not_snapshot(self):
        self.claim.status = ClaimRequest.Status.COMPLETED
        self.claim.save(update_fields=['status'])
        fields = dict(requester=self.requester, request_date=self.claim.request_date,
            status=ClaimRequest.Status.COMPLETED, requested_total='100', actual_total='80')
        ClaimRequest.objects.create(**fields)
        for changes in ({'status': ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW},
                        {'status': ClaimRequest.Status.CANCELLED}, {'actual_total': None},
                        {'actual_total': Decimal('0')}, {'request_date': date(2026, 10, 23)},
                        {'requester': User.objects.create_user(username='settlement-day-other')}):
            ClaimRequest.objects.create(**{**fields, **changes})
        self.claim.daily_requested_total = Decimal('9999')
        text = self.message()
        self.assertIn('ยอดใช้จริงสะสมของวันที่ขอเบิก (24/10/2026): 5,580.00 บาท', text)
        self.assertNotIn('9,999.00', text)
        self.assertNotIn('ยอดสะสมสำหรับอนุมัติ', text)
        self.assertIn('เฉพาะคำขอของผู้ขอคนนี้ที่ปิดรายการแล้วและมียอดใช้จริง', text)

    def test_missing_payment_snapshot_and_acknowledgement_do_not_invent_daily_context(self):
        self.claim.excess_daily_total = None
        self.assertNotIn('ยอดสะสมสำหรับอนุมัติ', self.message())
        self.claim.excess_daily_total = Decimal('5580')
        self.assertNotIn('ยอดสะสม', self.message(cfo_ack_stage='excess'))
