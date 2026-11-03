from datetime import date
from decimal import Decimal
import json
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.roles import SUPERVISOR_APPROVER
from apps.core.management.commands.send_daily_pending_reminders import responsible_user
from apps.core.models import Budget, Notification
from apps.core.system_settings import get_system_settings
from apps.core.tracking import _tracking_card
from .models import ClaimRequest
from .views import process_telegram_approval, telegram_approval_callback_data


@override_settings(TELEGRAM_BOT_TOKEN='')
class DailyAndExcessCfoTests(TestCase):
    def setUp(self):
        # ตรึงวันที่เฉพาะชุดเดิมที่ทดสอบเรื่องอื่น ไม่ให้วันที่ตัวอย่างเก่าหมดอายุ
        self.policy_clock = patch('apps.claims.policies.current_date', return_value=date(2026, 9, 19))
        self.policy_clock.start()
        self.addCleanup(self.policy_clock.stop)
        self.requester = User.objects.create_user(username='daily-requester', department='IT')
        self.supervisor = User.objects.create_user(username='daily-supervisor')
        self.supervisor.groups.add(Group.objects.get(name=SUPERVISOR_APPROVER))
        self.manager = User.objects.create_user(username='daily-accounting-manager')
        self.accountant = User.objects.create_user(username='daily-accountant')
        self.cfo = User.objects.create_user(username='daily-cfo')
        for user in (self.requester, self.supervisor, self.manager, self.accountant, self.cfo):
            user.signature.name = 'signatures/test/signature.png'
            user.save(update_fields=['signature'])
        config = get_system_settings()
        config.cfo = self.cfo
        config.executive_approval_threshold = Decimal('5000.00')
        config.save(update_fields=['cfo', 'executive_approval_threshold'])
        Budget.objects.create(owner=self.accountant, department='IT', name='Office')

    def make_claim(self, amount, *, requester=None, request_date=date(2026, 10, 19), status=ClaimRequest.Status.DRAFT):
        claim = ClaimRequest.objects.create(
            requester=requester or self.requester, supervisor=self.supervisor, accounting_manager=self.manager,
            accountant=self.accountant, cfo=self.cfo, request_type=ClaimRequest.RequestType.ADVANCE,
            request_date=request_date, expense_month=request_date.replace(day=1), status=status, purpose='CFO workflow test', work_location='HQ',
        )
        claim.items.create(description='Office item', business_unit='BU', department='IT', budget='Office', requested_amount=amount)
        claim.refresh_requested_total()
        return claim

    def submit(self, claim):
        self.client.force_login(claim.requester)
        self.assertEqual(self.client.post(reverse('claim_submit', args=[claim.pk])).status_code, 302)
        claim.refresh_from_db()

    def approve(self, claim, actor):
        self.client.force_login(actor)
        self.assertEqual(self.client.post(reverse('claim_approval_action', args=[claim.pk]), {'action': 'approve'}).status_code, 302)
        claim.refresh_from_db()

    def submit_actual(self, claim, amount):
        claim.status = ClaimRequest.Status.PENDING_CLEARANCE
        claim.save(update_fields=['status'])
        claim.attachments.create(file='claim-attachments/test.pdf', original_name='test.pdf', uploaded_by=claim.requester)
        self.client.force_login(claim.requester)
        self.client.post(reverse('claim_clearance_submit', args=[claim.pk]), {f'actual_{claim.items.get().pk}': amount})
        claim.refresh_from_db()

    def test_daily_threshold_exactly_5000_and_only_crossing_request_requires_cfo(self):
        first = self.make_claim('3000.00')
        self.submit(first)
        second = self.make_claim('2000.00')
        self.submit(second)
        self.assertEqual(second.daily_requested_total, Decimal('5000.00'))
        self.assertFalse(second.requires_cfo_approval)
        third = self.make_claim('0.01')
        self.submit(third)
        self.assertEqual(third.daily_requested_total, Decimal('5000.01'))
        self.assertTrue(third.requires_cfo_approval)
        first.refresh_from_db()
        self.assertFalse(first.requires_cfo_approval)
        self.approve(third, self.supervisor)
        self.approve(third, self.cfo)
        self.assertIsNone(third.cfo_approved_at)
        self.approve(third, self.manager)
        self.assertEqual(third.current_approver(), self.cfo)
        self.assertEqual(Notification.objects.filter(recipient=self.cfo, title='รอ CFO อนุมัติ').count(), 1)
        self.approve(third, self.cfo)
        self.assertEqual(third.status, ClaimRequest.Status.PENDING_PAYMENT)

    def test_daily_total_excludes_unsent_and_unsuccessful_other_people_and_dates(self):
        for status in (ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED, ClaimRequest.Status.REJECTED, ClaimRequest.Status.CANCELLED):
            self.make_claim('9000.00', status=status)
        other = User.objects.create_user(username='different-requester', department='IT')
        self.make_claim('9000.00', requester=other, status=ClaimRequest.Status.PENDING_SUPERVISOR)
        self.make_claim('9000.00', request_date=date(2026, 10, 18), status=ClaimRequest.Status.COMPLETED)
        self.make_claim('3000.00', status=ClaimRequest.Status.COMPLETED)
        claim = self.make_claim('2000.00')
        self.submit(claim)
        self.assertEqual(claim.daily_requested_total, Decimal('5000.00'))
        self.assertFalse(claim.requires_cfo_approval)

    def test_completed_actual_80_plus_4950_snapshots_5030_and_requires_cfo(self):
        previous = self.make_claim('100', status=ClaimRequest.Status.COMPLETED)
        previous.actual_total = Decimal('80')
        previous.save(update_fields=['actual_total'])
        claim = self.make_claim('4950')
        self.submit(claim)
        self.assertEqual(claim.daily_requested_total, Decimal('5030'))
        self.assertTrue(claim.requires_cfo_approval)

    def test_completed_zero_and_legacy_missing_actual_and_unfinished_refund(self):
        previous = self.make_claim('100', status=ClaimRequest.Status.COMPLETED)
        claim = self.make_claim('4950')
        for status, actual, expected in (
            (ClaimRequest.Status.COMPLETED, Decimal('0'), Decimal('4950')),
            (ClaimRequest.Status.COMPLETED, None, Decimal('5050')),
            (ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW, Decimal('80'), Decimal('5050')),
            (ClaimRequest.Status.PENDING_EXCESS_APPROVAL, Decimal('150'), Decimal('5100')),
            (ClaimRequest.Status.COMPLETED, Decimal('50'), Decimal('5000')),
        ):
            previous.status, previous.actual_total = status, actual
            previous.save(update_fields=['status', 'actual_total'])
            self.assertEqual(claim.calculate_daily_total(), expected)

    def test_resubmission_recalculates_daily_total_without_double_counting(self):
        claim = self.make_claim('4000.00')
        self.submit(claim)
        other = self.make_claim('2000.00')
        self.submit(other)
        claim.status = ClaimRequest.Status.RETURNED
        claim.cfo_approved_at = timezone.now()
        claim.save(update_fields=['status', 'cfo_approved_at'])
        item = claim.items.get()
        item.requested_amount = Decimal('1000.00')
        item.save(update_fields=['requested_amount'])
        claim.refresh_requested_total()
        self.submit(claim)
        self.assertEqual(claim.daily_requested_total, Decimal('3000.00'))
        self.assertFalse(claim.requires_cfo_approval)
        self.assertIsNone(claim.cfo_approved_at)

    def test_new_submission_includes_known_supplemental_amounts(self):
        previous = self.make_claim('3000.00', status=ClaimRequest.Status.COMPLETED)
        previous.actual_total = Decimal('3500.00')
        previous.save(update_fields=['actual_total'])
        claim = self.make_claim('1600.00')
        self.submit(claim)
        self.assertEqual(claim.daily_requested_total, Decimal('5100.00'))
        self.assertTrue(claim.requires_cfo_approval)

    @patch('apps.claims.views.send_telegram_notification')
    def test_4000_actual_5500_requires_ordered_manager_and_cfo_excess_approval(self, telegram):
        claim = self.make_claim('4000.00')
        self.submit(claim)
        self.approve(claim, self.supervisor)
        self.approve(claim, self.manager)
        self.submit_actual(claim, '5500.00')
        self.assertTrue(claim.requires_cfo_excess_approval)
        self.assertEqual(claim.current_approver(), self.manager)
        self.assertFalse(claim.can_cfo_acknowledge(self.cfo, 'excess'))
        self.approve(claim, self.cfo)
        self.assertIsNone(claim.excess_cfo_approved_at)
        self.approve(claim, self.manager)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_EXCESS_APPROVAL)
        self.assertIsNotNone(claim.excess_accounting_approved_at)
        self.assertEqual(claim.current_approver(), self.cfo)
        self.assertEqual(claim.workflow_status_display, 'รอ CFO อนุมัติส่วนต่าง')
        self.assertEqual(responsible_user(claim), self.cfo)
        self.assertEqual(_tracking_card(claim)['waiting_for'], self.cfo.username)
        self.assertEqual(Notification.objects.filter(recipient=self.cfo, title='รอ CFO อนุมัติส่วนต่าง').count(), 1)
        self.assertFalse(Notification.objects.filter(recipient=self.cfo, title='CFO รับทราบยอดใช้เกิน').exists())
        messages = [str(call.args) for call in telegram.call_args_list]
        self.assertTrue(any('5,500.00' in message and '1,500.00' in message for message in messages))
        self.client.force_login(self.cfo)
        self.assertContains(self.client.get(reverse('claim_list') + '?queue=cfo_approval'), 'CFO workflow test')
        self.assertContains(self.client.get(reverse('approval_queue')), 'CFO workflow test')
        dashboard = self.client.get(reverse('dashboard'))
        card = next(card for card in dashboard.context['status_cards'] if card['code'] == 'CFO_APPROVAL')
        self.assertEqual(card['count'], 1)
        self.client.force_login(self.accountant)
        self.client.post(reverse('claim_payment', args=[claim.pk]), {'payment_method': 'CASH'})
        self.assertFalse(claim.payments.exists())
        self.assertEqual(process_telegram_approval(claim, self.manager, 'approve'), 'รายการนี้ดำเนินการไปแล้ว')
        self.assertEqual(process_telegram_approval(claim, self.cfo, 'approve'), 'อนุมัติคำขอเรียบร้อยแล้ว')
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertIsNotNone(claim.excess_cfo_approved_at)
        self.assertIsNone(claim.cfo_approved_at)

    def test_6000_actual_7000_requires_a_second_cfo_approval(self):
        claim = self.make_claim('6000.00')
        self.submit(claim)
        self.approve(claim, self.supervisor)
        self.approve(claim, self.manager)
        self.approve(claim, self.cfo)
        initial_approval = claim.cfo_approved_at
        self.submit_actual(claim, '7000.00')
        self.approve(claim, self.manager)
        self.assertEqual(claim.current_approver(), self.cfo)
        self.assertIsNone(claim.excess_cfo_approved_at)
        self.approve(claim, self.cfo)
        self.assertEqual(claim.cfo_approved_at, initial_approval)
        self.assertIsNotNone(claim.excess_cfo_approved_at)

    @patch('apps.claims.views.answer_telegram_callback')
    @patch('apps.claims.views.remove_telegram_callback_keyboard')
    def test_telegram_cfo_button_only_works_after_excess_manager_approval(self, remove_keyboard, answer):
        self.cfo.telegram_user_id = '991188'
        self.cfo.save(update_fields=['telegram_user_id'])
        claim = self.make_claim('4000.00')
        self.submit_actual(claim, '5500.00')
        payload = {'callback_query': {'id': 'excess-cfo', 'data': telegram_approval_callback_data(claim, 'approve', actor=self.cfo), 'from': {'id': 991188}}}
        with self.settings(TELEGRAM_WEBHOOK_SECRET='test-cfo-secret'):
            self.client.post(reverse('telegram_webhook'), data=json.dumps(payload), content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='test-cfo-secret')
            claim.refresh_from_db()
            self.assertIsNone(claim.excess_cfo_approved_at)
            answer.assert_called_with('excess-cfo', 'ปุ่มนี้หมดอายุแล้ว กรุณาใช้ข้อความอนุมัติขั้นล่าสุด')
            self.approve(claim, self.manager)
            payload['callback_query']['id'] = 'excess-cfo-new'
            payload['callback_query']['data'] = telegram_approval_callback_data(claim, 'approve', actor=self.cfo)
            self.client.post(reverse('telegram_webhook'), data=json.dumps(payload), content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='test-cfo-secret')
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertIsNotNone(claim.excess_cfo_approved_at)

    def test_small_excess_below_threshold_only_requires_cfo_acknowledgement(self):
        claim = self.make_claim('4000.00')
        self.submit_actual(claim, '4500.00')
        self.assertFalse(claim.requires_cfo_excess_approval)
        self.approve(claim, self.manager)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertTrue(claim.can_cfo_acknowledge(self.cfo, 'excess'))

    def test_small_excess_crosses_daily_threshold_with_another_request(self):
        self.make_claim('3000.00', status=ClaimRequest.Status.COMPLETED)
        claim = self.make_claim('2000.00')
        self.submit(claim)
        self.assertFalse(claim.requires_cfo_approval)
        self.submit_actual(claim, '2100.00')
        self.assertEqual(claim.excess_daily_total, Decimal('5100.00'))
        self.assertTrue(claim.requires_cfo_excess_approval)

    def test_manager_requester_excess_requires_cfo_even_below_5000(self):
        self.requester.position_level = User.PositionLevel.MANAGER
        self.requester.save(update_fields=['position_level'])
        claim = self.make_claim('100.00')
        self.submit_actual(claim, '125.00')
        self.assertTrue(claim.requires_cfo_excess_approval)
        self.approve(claim, self.manager)
        self.assertEqual(claim.current_approver(), self.cfo)

    def test_accountant_correction_keeps_the_already_approved_excess_route(self):
        claim = self.make_claim('4000.00')
        self.submit_actual(claim, '4500.00')
        self.approve(claim, self.manager)
        self.client.force_login(self.accountant)
        self.client.post(reverse('claim_clearance_correct', args=[claim.pk]), {f'corrected_actual_{claim.items.get().pk}': '5500.00'})
        claim.refresh_from_db()
        self.assertEqual(claim.actual_total, Decimal('5500.00'))
        self.assertFalse(claim.requires_cfo_excess_approval)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
