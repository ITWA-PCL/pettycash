from datetime import timedelta
from decimal import Decimal
from io import StringIO
import json
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.management.commands.send_daily_pending_reminders import responsible_user
from apps.core.models import Notification
from apps.core.system_settings import get_system_settings
from apps.core.tracking import _tracking_card
from .models import ClaimApproval, ClaimPayment, ClaimRequest
from .views import telegram_action_keyboard


@override_settings(TELEGRAM_BOT_TOKEN='')
class SupplementalReceiptTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username='supplemental-requester', telegram_user_id='773301')
        self.accountant = User.objects.create_user(username='supplemental-accountant')
        self.other = User.objects.create_user(username='supplemental-other')
        for user in (self.requester, self.accountant, self.other):
            user.signature.name = 'signatures/test/signature.png'
            user.save(update_fields=['signature'])
        self.config = get_system_settings()

    def make_claim(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, accountant=self.accountant, supervisor=self.other, accounting_manager=self.other, work_location='HQ',
            request_type=ClaimRequest.RequestType.ADVANCE, purpose='Supplemental receipt test',
            requested_total=Decimal('6000.00'), actual_total=Decimal('7000.00'),
            status=ClaimRequest.Status.PENDING_PAYMENT,
        )
        claim.items.create(description='Expense', requested_amount='6000.00', actual_amount='7000.00')
        initial = claim.approval_records.create(actor=self.accountant, action=ClaimApproval.Action.PAID,
            status_before=ClaimRequest.Status.PENDING_PAYMENT, status_after=ClaimRequest.Status.PAID)
        ClaimApproval.objects.filter(pk=initial.pk).update(created_at=timezone.now() - timedelta(days=10))
        received = claim.approval_records.create(actor=self.requester, action=ClaimApproval.Action.RECEIVED,
            status_before=ClaimRequest.Status.PAID, status_after=ClaimRequest.Status.PENDING_CLEARANCE)
        ClaimApproval.objects.filter(pk=received.pk).update(created_at=timezone.now() - timedelta(days=9))
        claim.payments.create(kind=ClaimPayment.Kind.INITIAL, method=ClaimPayment.Method.CASH, amount='6000.00', paid_by=self.accountant)
        return claim

    def pay(self, claim):
        self.client.force_login(self.accountant)
        self.client.post(reverse('claim_payment', args=[claim.pk]), {'payment_method': 'CASH'})
        claim.refresh_from_db()
        return claim.latest_payment_event

    def confirm(self, claim, event_id):
        self.client.force_login(self.requester)
        self.client.post(reverse('claim_receipt_confirm', args=[claim.pk]), {'payment_event_id': event_id})
        claim.refresh_from_db()

    def test_supplemental_payment_waits_for_receipt_of_only_the_difference(self):
        claim = self.make_claim()
        event = self.pay(claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.assertEqual(event.action, ClaimApproval.Action.SUPPLEMENTAL_PAID)
        self.assertEqual(claim.receipt_amount, Decimal('1000.00'))
        self.assertEqual(claim.workflow_status_display, 'รอยืนยันรับเงินส่วนต่าง')
        payment = claim.payments.get(kind=ClaimPayment.Kind.SUPPLEMENTAL)
        self.assertEqual(payment.amount, Decimal('1000.00'))
        message = Notification.objects.get(recipient=self.requester, title='จ่ายเงินส่วนต่างแล้ว').message
        self.assertIn('1,000.00', message)
        self.assertIn('ภายใน 1 วัน', message)
        self.assertFalse(Notification.objects.filter(title='คำขอเสร็จสิ้น').exists())
        self.client.force_login(self.requester)
        detail = self.client.get(reverse('claim_detail', args=[claim.pk]))
        self.assertContains(detail, 'ยืนยันรับเงินส่วนต่าง')
        self.assertContains(detail, 'ยอดที่รอยืนยัน 1000.00 บาท')
        self.assertContains(detail, f'name="payment_event_id" value="{event.pk}"')
        self.assertNotContains(detail, 'เปิดเอกสารสำหรับพิมพ์')
        self.assertEqual(responsible_user(claim), self.requester)
        tracker = _tracking_card(claim)
        self.assertEqual(tracker['headline'], 'รอยืนยันรับเงินส่วนต่าง')
        self.assertEqual(tracker['steps'][-1]['state'], 'current')

    def test_initial_and_supplemental_disbursements_require_separate_receipts(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, accountant=self.accountant, supervisor=self.other, accounting_manager=self.other,
            request_type=ClaimRequest.RequestType.ADVANCE, status=ClaimRequest.Status.PENDING_PAYMENT,
            requested_total='100.00', work_location='HQ', purpose='Full receipt cycle',
        )
        item = claim.items.create(description='Expense', requested_amount='100.00')
        initial = self.pay(claim)
        self.confirm(claim, initial.pk)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_CLEARANCE)
        claim.attachments.create(file='claim-attachments/test.pdf', original_name='test.pdf', uploaded_by=self.requester)
        self.client.post(reverse('claim_clearance_submit', args=[claim.pk]), {f'actual_{item.pk}': '125.00'})
        self.client.force_login(self.other)
        self.client.post(reverse('claim_approval_action', args=[claim.pk]), {'action': 'approve'})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        supplemental = self.pay(claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.assertNotEqual(initial.pk, supplemental.pk)
        self.confirm(claim, initial.pk)
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.confirm(claim, supplemental.pk)
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)
        self.assertEqual(claim.approval_records.filter(action=ClaimApproval.Action.RECEIVED).count(), 2)
        self.assertEqual(list(claim.payments.values_list('amount', flat=True)), [Decimal('100.00'), Decimal('25.00')])

    def test_manual_receipt_closes_once_without_returning_to_clearance(self):
        claim = self.make_claim()
        event = self.pay(claim)
        self.confirm(claim, event.pk)
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)
        receipt = claim.approval_records.filter(action=ClaimApproval.Action.RECEIVED).last()
        self.assertEqual(receipt.status_after, ClaimRequest.Status.COMPLETED)
        self.assertEqual(receipt.actor, self.requester)
        self.assertIn('ส่วนต่าง 1,000.00', receipt.comment)
        self.confirm(claim, event.pk)
        call_command('auto_confirm_receipts', stdout=StringIO())
        self.assertEqual(claim.approval_records.filter(action=ClaimApproval.Action.RECEIVED).count(), 2)
        self.assertFalse(claim.approval_records.filter(action=ClaimApproval.Action.AUTO_RECEIVED).exists())

    def test_old_initial_receipt_and_missing_token_cannot_confirm_supplemental(self):
        claim = self.make_claim()
        initial = claim.latest_payment_event
        self.pay(claim)
        self.confirm(claim, initial.pk)
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.client.post(reverse('claim_receipt_confirm', args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.client.force_login(self.accountant)
        self.client.post(reverse('claim_receipt_confirm', args=[claim.pk]), {'payment_event_id': claim.latest_payment_event.pk})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.assertEqual(claim.approval_records.filter(action=ClaimApproval.Action.RECEIVED).count(), 1)

    @patch('apps.claims.views.answer_telegram_callback')
    @patch('apps.claims.views.remove_telegram_callback_keyboard')
    def test_telegram_receipt_is_scoped_to_supplemental_payment(self, remove_keyboard, answer):
        claim = self.make_claim()
        initial = claim.latest_payment_event
        event = self.pay(claim)
        keyboard = telegram_action_keyboard(claim)
        button = keyboard['inline_keyboard'][0][0]
        self.assertIn('ส่วนต่าง', button['text'])
        self.assertEqual(button['callback_data'], f'pc:{claim.pk}:receive-{event.pk}')
        with self.settings(TELEGRAM_WEBHOOK_SECRET='supplemental-secret'):
            for action in ('receive', f'receive-{initial.pk}', f'receive-{event.pk}', f'receive-{event.pk}'):
                payload = {'callback_query': {'id': f'supplemental-click-{action}', 'data': f'pc:{claim.pk}:{action}', 'from': {'id': 773301}}}
                response = self.client.post(reverse('telegram_webhook'), data=json.dumps(payload), content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='supplemental-secret')
                self.assertEqual(response.status_code, 200)
                claim.refresh_from_db()
                self.assertEqual(claim.status, ClaimRequest.Status.PAID if action in ('receive', f'receive-{initial.pk}') else ClaimRequest.Status.COMPLETED)
        self.assertEqual(claim.approval_records.filter(action=ClaimApproval.Action.RECEIVED).count(), 2)

    def test_auto_receipt_uses_exactly_24_hours_from_supplemental_payment(self):
        claim = self.make_claim()
        event = self.pay(claim)
        now = timezone.now()
        ClaimApproval.objects.filter(pk=event.pk).update(created_at=now - timedelta(days=1) + timedelta(seconds=1))
        with patch('apps.core.management.commands.auto_confirm_receipts.timezone.now', return_value=now):
            call_command('auto_confirm_receipts', stdout=StringIO())
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.PAID)
            ClaimApproval.objects.filter(pk=event.pk).update(created_at=now - timedelta(days=1))
            call_command('auto_confirm_receipts', stdout=StringIO())
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)
        auto = claim.approval_records.get(action=ClaimApproval.Action.AUTO_RECEIVED)
        self.assertIsNone(auto.actor)
        self.assertIn('ครบ 1 วัน', auto.comment)
        self.assertIn('ส่วนต่าง 1,000.00', auto.comment)
        call_command('auto_confirm_receipts', stdout=StringIO())
        self.assertEqual(claim.approval_records.filter(action=ClaimApproval.Action.AUTO_RECEIVED).count(), 1)
        self.client.force_login(self.requester)
        printed = self.client.get(reverse('claim_print', args=[claim.pk]))
        self.assertContains(printed, 'ผู้ขอไม่ได้กดยืนยัน')
        self.assertNotContains(printed, 'alt="ลายเซ็นผู้รับเงิน"')

    def test_supplemental_deadline_stays_one_day_when_initial_advance_is_three_days(self):
        self.config.auto_receive_after_days = 3
        self.config.save(update_fields=['auto_receive_after_days'])
        claim = self.make_claim()
        event = self.pay(claim)
        ClaimApproval.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(hours=25))
        call_command('auto_confirm_receipts', stdout=StringIO())
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)

    def test_disabled_automation_and_dry_run_preserve_pending_receipt(self):
        claim = self.make_claim()
        event = self.pay(claim)
        ClaimApproval.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(days=2))
        call_command('auto_confirm_receipts', dry_run=True, stdout=StringIO())
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.config.auto_receive_enabled = False
        self.config.save(update_fields=['auto_receive_enabled'])
        call_command('auto_confirm_receipts', stdout=StringIO())
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)

    def test_historical_completed_supplemental_claim_is_not_reopened(self):
        claim = self.make_claim()
        self.pay(claim)
        claim.status = ClaimRequest.Status.COMPLETED
        claim.save(update_fields=['status'])
        call_command('auto_confirm_receipts', stdout=StringIO())
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)

    def test_nonblocking_cfo_acknowledgement_stays_available_while_waiting_for_receipt(self):
        cfo = User.objects.create_user(username='supplemental-cfo')
        claim = self.make_claim()
        claim.cfo = cfo
        claim.excess_cfo_required = False
        claim.save(update_fields=['cfo', 'excess_cfo_required'])
        self.pay(claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.assertTrue(claim.can_cfo_acknowledge(cfo, 'excess'))
