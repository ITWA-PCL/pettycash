from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.models import AdminAuditLog, Notification
from apps.core.system_settings import get_system_settings
from .models import ClaimApproval, ClaimPayment, ClaimRequest


@override_settings(TELEGRAM_BOT_TOKEN='')
class PrepaymentCorrectionTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username='prepay-requester')
        self.accountant = User.objects.create_user(username='prepay-accountant')
        self.manager = User.objects.create_user(username='prepay-manager')
        self.cfo = User.objects.create_user(username='prepay-cfo')
        for user in (self.requester, self.accountant, self.manager, self.cfo):
            user.signature.name = 'signatures/test/prepay.png'
            user.save()
        config = get_system_settings()
        config.cfo = self.cfo
        config.save()

    def make_claim(self, total='200.00', **kwargs):
        fields = dict(requester=self.requester, accountant=self.accountant,
            accounting_manager=self.manager, cfo=self.cfo, supervisor=self.manager,
            request_type='PETTY_CASH', work_location='HQ', purpose='Correct before paying',
            status=ClaimRequest.Status.PENDING_PAYMENT, request_date=date(2026, 10, 22),
            expense_month=date(2026, 11, 1), requested_total=Decimal(total), daily_requested_total=Decimal(total),
            approval_threshold=Decimal('5000'), accounting_approved_at=timezone.now(),
            cfo_acknowledged_at=timezone.now())
        fields.update(kwargs)
        claim = ClaimRequest.objects.create(**fields)
        for name in ('Postage', 'Supplies'):
            claim.items.create(description=name, requested_amount=Decimal(total)/2)
        return claim

    def correction_payload(self, claim, amounts):
        data = {'amount_revision': str(claim.amount_revision), 'correction_note': 'Checked receipt'}
        data.update({f'corrected_requested_{item.pk}': amount for item, amount in zip(claim.items.all(), amounts)})
        return data

    def correct(self, claim, amounts, actor=None):
        self.client.force_login(actor or self.accountant)
        response = self.client.post(reverse('claim_amount_correct', args=[claim.pk]), self.correction_payload(claim, amounts))
        claim.refresh_from_db()
        return response

    def pay(self, claim, revision=None):
        self.client.force_login(self.accountant)
        data = {'payment_method': 'CASH'}
        if revision is not None:
            data['amount_revision'] = str(revision)
        response = self.client.post(reverse('claim_payment', args=[claim.pk]), data)
        claim.refresh_from_db()
        return response

    def approve(self, claim, actor):
        self.client.force_login(actor)
        self.client.post(reverse('claim_approval_action', args=[claim.pk]), {'action': 'approve'})
        claim.refresh_from_db()

    def test_small_correction_is_audited_then_pays_corrected_full_amount_and_waits_for_receipt(self):
        claim = self.make_claim()
        response = self.correct(claim, ['125', '125'])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(claim.requested_total, Decimal('250'))
        self.assertEqual(claim.daily_requested_total, Decimal('250'))
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertIsNotNone(claim.accounting_approved_at)
        self.assertFalse(claim.requires_cfo_approval)
        self.assertEqual(claim.amount_revision, 1)
        self.assertIsNone(claim.actual_total)
        self.assertEqual(claim.request_date, date(2026, 10, 22))
        self.assertEqual(claim.expense_month, date(2026, 11, 1))
        record = claim.approval_records.get(action=ClaimApproval.Action.AMOUNT_CORRECTED)
        self.assertIn('200.00 → 250.00', record.comment)
        self.assertIn('Postage: 100.00 → 125.00', record.comment)
        self.assertIn('Checked receipt', record.comment)
        self.assertTrue(AdminAuditLog.objects.filter(action='CORRECT_REQUESTED', actor=self.accountant).exists())
        self.pay(claim, revision=1)
        payment = claim.payments.get()
        self.assertEqual(payment.kind, ClaimPayment.Kind.INITIAL)
        self.assertEqual(payment.amount, Decimal('250'))
        self.assertEqual(claim.actual_total, Decimal('250'))
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.assertEqual(claim.receipt_amount, Decimal('250'))
        self.client.force_login(self.requester)
        self.client.post(reverse('claim_receipt_confirm', args=[claim.pk]), {'payment_event_id': claim.latest_payment_event.pk})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)

    def test_crossing_threshold_requires_accounting_manager_then_cfo_before_payment(self):
        claim = self.make_claim('4000')
        self.correct(claim, ['2750', '2750'])
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.assertTrue(claim.requires_cfo_approval)
        self.assertIsNone(claim.accounting_approved_at)
        self.assertIsNone(claim.cfo_approved_at)
        self.assertIsNone(claim.cfo_acknowledged_at)
        self.assertEqual(claim.current_approver(), self.manager)
        self.assertTrue(Notification.objects.filter(recipient=self.manager).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.cfo).exists())
        self.pay(claim, revision=1)
        self.assertFalse(claim.payments.exists())
        self.approve(claim, self.cfo)
        self.assertIsNone(claim.cfo_approved_at)
        self.approve(claim, self.manager)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.assertEqual(claim.current_approver(), self.cfo)
        self.approve(claim, self.cfo)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.pay(claim, revision=1)
        self.assertEqual(claim.payments.get().amount, Decimal('5500'))

    def test_daily_total_crossing_threshold_requires_cfo_even_if_this_claim_is_small(self):
        other = self.make_claim('4000', status=ClaimRequest.Status.COMPLETED)
        claim = self.make_claim()
        self.correct(claim, ['600', '600'])
        self.assertEqual(claim.daily_requested_total, Decimal('5200'))
        self.assertTrue(claim.requires_cfo_approval)
        self.assertEqual(claim.current_approver(), self.manager)
        other.refresh_from_db()
        self.assertEqual(other.status, ClaimRequest.Status.COMPLETED)

    def test_formerly_cfo_approved_claim_still_requires_cfo_after_decrease(self):
        claim = self.make_claim('6000', cfo_approved_at=timezone.now())
        claim.approval_records.create(actor=self.cfo, action=ClaimApproval.Action.APPROVED,
            status_before=ClaimRequest.Status.PENDING_ACCOUNTING, status_after=ClaimRequest.Status.PENDING_PAYMENT)
        with patch('apps.claims.views.send_telegram_notification') as telegram:
            self.correct(claim, ['2000', '2000'])
        message = telegram.call_args_list[-1].args[2]
        self.assertNotIn(f'{self.cfo.username} (CFO)', message)
        self.assertIn('อนุมัติยอดใหม่', message)
        self.assertNotIn('ยอดขอสะสมหลังแก้ยอด', message)
        self.assertEqual(claim.requested_total, Decimal('4000'))
        self.assertEqual(claim.daily_requested_total, Decimal('4000'))
        self.assertTrue(claim.corrected_cfo_required)
        self.assertTrue(claim.requires_cfo_approval)
        self.approve(claim, self.manager)
        self.assertEqual(claim.current_approver(), self.cfo)

    def test_exactly_threshold_does_not_add_cfo_and_small_decrease_can_pay(self):
        claim = self.make_claim('4000')
        self.correct(claim, ['2500', '2500'])
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertFalse(claim.requires_cfo_approval)
        self.correct(claim, ['50', '50'])
        self.assertEqual(claim.requested_total, Decimal('100'))
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)

    def test_manager_requester_still_requires_cfo_on_correction(self):
        claim = self.make_claim(requester_position_level=User.PositionLevel.MANAGER)
        self.correct(claim, ['125', '125'])
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.assertTrue(claim.requires_cfo_approval)

    def test_invalid_amounts_and_total_overflow_never_partially_change_items(self):
        claim = self.make_claim()
        for amounts in (['125', 'NaN'], ['125', 'Infinity'], ['125', '-1'], ['125', '0'],
                        ['125', '1.001'], ['125', ''], ['9999999999.99', '9999999999.99']):
            self.correct(claim, amounts)
            self.assertEqual(claim.requested_total, Decimal('200'))
            self.assertEqual(claim.amount_revision, 0)
            self.assertEqual(list(claim.items.values_list('requested_amount', flat=True)), [Decimal('100'), Decimal('100')])
        self.assertFalse(claim.approval_records.exists())

    def test_same_values_are_noop_but_changed_line_distribution_is_audited(self):
        claim = self.make_claim()
        self.correct(claim, ['100', '100'])
        self.assertEqual(claim.amount_revision, 0)
        self.assertFalse(claim.approval_records.exists())
        self.correct(claim, ['125', '75'])
        self.assertEqual(claim.requested_total, Decimal('200'))
        self.assertEqual(claim.amount_revision, 1)
        self.assertEqual(claim.approval_records.count(), 1)

    def test_stale_or_duplicate_correction_and_payment_forms_are_rejected(self):
        claim = self.make_claim()
        stale = self.correction_payload(claim, ['150', '150'])
        self.correct(claim, ['125', '125'])
        self.client.post(reverse('claim_amount_correct', args=[claim.pk]), stale)
        claim.refresh_from_db()
        self.assertEqual(claim.requested_total, Decimal('250'))
        self.assertEqual(claim.amount_revision, 1)
        self.assertEqual(claim.approval_records.count(), 1)
        for revision in (None, 0):
            self.pay(claim, revision)
            self.assertFalse(claim.payments.exists())
        self.pay(claim, 1)
        self.assertEqual(claim.payments.count(), 1)
        self.correct(claim, ['150', '150'])
        self.assertEqual(claim.requested_total, Decimal('250'))

    def test_requester_and_other_roles_cannot_edit_even_superuser_requester(self):
        claim = self.make_claim()
        for actor in (self.requester, self.manager):
            self.assertIn(self.correct(claim, ['125', '125'], actor).status_code, (403, 404))
        self.requester.is_superuser = True
        self.requester.save()
        self.assertEqual(self.correct(claim, ['125', '125'], self.requester).status_code, 403)
        self.assertEqual(claim.requested_total, Decimal('200'))

    def test_cannot_correct_advance_other_states_or_already_paid_record(self):
        for fields in ({'request_type': 'ADVANCE'}, {'status': ClaimRequest.Status.PAID},
                       {'status': ClaimRequest.Status.COMPLETED}, {'actual_total': Decimal('200')}):
            claim = self.make_claim(**fields)
            self.correct(claim, ['125', '125'])
            self.assertEqual(claim.requested_total, Decimal('200'))
        claim = self.make_claim()
        claim.approval_records.create(actor=self.accountant, action=ClaimApproval.Action.PAID,
            status_before=ClaimRequest.Status.PENDING_PAYMENT, status_after=ClaimRequest.Status.PAID)
        self.correct(claim, ['125', '125'])
        self.assertEqual(claim.requested_total, Decimal('200'))

    def test_get_and_csrf_cannot_change_amounts(self):
        claim = self.make_claim()
        self.client.force_login(self.accountant)
        url = reverse('claim_amount_correct', args=[claim.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.accountant)
        self.assertEqual(client.post(url, self.correction_payload(claim, ['125', '125'])).status_code, 403)
        claim.refresh_from_db()
        self.assertEqual(claim.requested_total, Decimal('200'))

    def test_ui_shows_correction_only_for_assigned_payer_before_payment(self):
        claim = self.make_claim()
        self.client.force_login(self.accountant)
        response = self.client.get(reverse('claim_detail', args=[claim.pk]))
        self.assertContains(response, 'แก้ไขยอดก่อนจ่าย')
        self.assertContains(response, 'name="amount_revision" value="0"')
        self.client.force_login(self.requester)
        self.assertNotContains(self.client.get(reverse('claim_detail', args=[claim.pk])), 'แก้ไขยอดก่อนจ่าย')
