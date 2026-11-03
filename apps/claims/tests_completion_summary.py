from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from .models import ClaimApproval, ClaimPayment, ClaimRequest
from .views import notify


@override_settings(TELEGRAM_BOT_TOKEN='')
class CompletionSummaryTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username='summary-requester', department='IT')
        self.accountant = User.objects.create_user(username='summary-accountant')
        self.accountant.signature.name = 'signatures/test/summary.png'
        self.accountant.save()

    def make_claim(self, *, actual='138.00', requested='200.00', request_type='ADVANCE', ledger=True, **kwargs):
        fields = dict(requester=self.requester, accountant=self.accountant, work_location='HQ',
            purpose='Final expense summary', request_type=request_type, status=ClaimRequest.Status.COMPLETED,
            requested_total=Decimal(requested), actual_total=Decimal(actual) if actual is not None else None,
            daily_requested_total=Decimal('450.00'))
        fields.update(kwargs)
        claim = ClaimRequest.objects.create(**fields)
        claim.items.create(description='Postage', budget='Postage budget', department='IT',
            requested_amount=Decimal(requested), actual_amount=claim.actual_total)
        if ledger:
            self.payment(claim, ClaimPayment.Kind.INITIAL, requested)
        return claim

    def payment(self, claim, kind, amount):
        return claim.payments.create(kind=kind, amount=amount, method=ClaimPayment.Method.CASH, paid_by=self.accountant)

    def summary(self, claim):
        with patch('apps.claims.views.send_telegram_notification') as bot, patch('apps.claims.views.send_telegram_group_notification') as group:
            notify(self.requester, 'คำขอเสร็จสิ้น', 'ตรวจยอดและปิดรายการแล้ว', claim)
        message = bot.call_args.args[2]
        self.assertEqual(message, group.call_args.args[1])
        return message

    def test_returned_money_and_actual_total_are_clear_in_bot_and_group(self):
        claim = self.make_claim()
        message = self.summary(claim)
        self.assertIn('ยอดใช้จริงรวม: 138.00 บาท', message)
        self.assertIn('รับเงินแล้ว: 200.00 บาท', message)
        self.assertNotIn('ยอดเบิกตามคำขอ:', message)
        self.assertIn('คืนเงินแล้ว: 62.00 บาท', message)
        self.assertIn('Postage: 138.00 บาท', message)
        self.assertIn('รายการใช้จริง:', message)
        self.assertNotIn('ยอดขอสะสม', message)
        self.assertNotIn('450.00', message)

    def test_zero_actual_is_not_replaced_by_requested_amount(self):
        message = self.summary(self.make_claim(actual='0'))
        self.assertIn('ยอดใช้จริงรวม: 0.00 บาท', message)
        self.assertIn('คืนเงินแล้ว: 200.00 บาท', message)
        self.assertIn('Postage: 0.00 บาท', message)

    def test_paid_supplemental_is_separate_from_actual_total(self):
        claim = self.make_claim(actual='250')
        self.payment(claim, ClaimPayment.Kind.SUPPLEMENTAL, '50')
        message = self.summary(claim)
        self.assertIn('ยอดใช้จริงรวม: 250.00 บาท', message)
        self.assertIn('รับเงินแล้ว: 250.00 บาท', message)
        self.assertIn('จ่ายเพิ่มแล้ว: 50.00 บาท', message)
        self.assertIn('คืนเงินแล้ว: 0.00 บาท', message)
        self.assertNotIn('ส่วนต่างที่ขอเพิ่ม', message)

    def test_return_uses_recorded_payments_including_supplemental(self):
        claim = self.make_claim(actual='250')
        self.payment(claim, ClaimPayment.Kind.SUPPLEMENTAL, '100')
        message = self.summary(claim)
        self.assertIn('ยอดใช้จริงรวม: 250.00 บาท', message)
        self.assertIn('รับเงินแล้ว: 300.00 บาท', message)
        self.assertIn('จ่ายเพิ่มแล้ว: 100.00 บาท', message)
        self.assertIn('คืนเงินแล้ว: 50.00 บาท', message)

    def test_corrected_out_of_pocket_completion_uses_final_amount(self):
        claim = self.make_claim(actual='250', requested='250', request_type='PETTY_CASH', amount_revision=1)
        message = self.summary(claim)
        self.assertIn('ยอดใช้จริงรวม: 250.00 บาท', message)
        self.assertIn('คืนเงินแล้ว: 0.00 บาท', message)
        self.assertNotIn('ส่วนต่างที่จ่ายเพิ่ม', message)

    def test_legacy_records_do_not_fabricate_unknown_actual_or_paid_supplemental(self):
        claim = self.make_claim(ledger=False)
        message = self.summary(claim)
        self.assertIn('คืนเงินแล้ว: 62.00 บาท', message)
        self.assertNotIn('ยอดจ่ายรวม:', message)
        claim = self.make_claim(actual=None, ledger=False)
        message = self.summary(claim)
        self.assertIn('ยอดใช้จริงรวม: ยังไม่ได้บันทึก', message)
        self.assertIn('Postage: ยังไม่ระบุยอดใช้จริง', message)
        self.assertNotIn('คืนเงินแล้ว:', message)
        claim = self.make_claim(actual='250', ledger=False)
        message = self.summary(claim)
        self.assertIn('ส่วนต่างที่ไม่พบประวัติจ่าย: 50.00 บาท', message)
        self.assertNotIn('จ่ายเพิ่มแล้ว:', message)
        claim.approval_records.create(actor=self.accountant, action=ClaimApproval.Action.SUPPLEMENTAL_PAID,
            status_before=ClaimRequest.Status.PENDING_PAYMENT, status_after=ClaimRequest.Status.COMPLETED)
        message = self.summary(claim)
        self.assertIn('จ่ายเพิ่มแล้ว: 50.00 บาท', message)

    def test_partial_legacy_payment_ledger_uses_approved_initial_amount(self):
        claim = self.make_claim(actual='250', ledger=False)
        self.payment(claim, ClaimPayment.Kind.SUPPLEMENTAL, '50')
        message = self.summary(claim)
        self.assertIn('รับเงินแล้ว: 250.00 บาท', message)
        self.assertIn('คืนเงินแล้ว: 0.00 บาท', message)

    def test_per_item_actuals_are_not_replaced_by_original_requested_values(self):
        claim = self.make_claim()
        item = claim.items.get()
        item.requested_amount, item.actual_amount = Decimal('100'), Decimal('60')
        item.save()
        claim.items.create(description='Supplies', department='IT', requested_amount='100', actual_amount='78')
        message = self.summary(claim)
        self.assertIn('ยอดใช้จริงรวม: 138.00 บาท', message)
        self.assertIn('Postage: 60.00 บาท', message)
        self.assertIn('Supplies: 78.00 บาท', message)

    def test_review_stage_shows_actual_and_pending_return_not_daily_context(self):
        message = self.summary(self.make_claim(status=ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW))
        self.assertIn('ยอดใช้จริงรวม: 138.00 บาท', message)
        self.assertIn('รับเงินแล้ว: 200.00 บาท', message)
        self.assertIn('ต้องคืนเงิน: 62.00 บาท', message)
        self.assertNotIn('ยอดขอเบิก:', message)
        self.assertNotIn('ยอดขอสะสม', message)
        self.assertNotIn('450.00', message)
        self.assertNotIn('คืนเงินแล้ว:', message)

    def test_real_accountant_close_sends_final_actual_and_return(self):
        claim = self.make_claim(status=ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW)
        self.client.force_login(self.accountant)
        with patch('apps.claims.views.send_telegram_notification') as bot, patch('apps.claims.views.send_telegram_group_notification') as group:
            self.client.post(reverse('claim_complete', args=[claim.pk]), {'refund_confirmed': '1'})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)
        message = bot.call_args.args[2]
        self.assertIn('ยอดใช้จริงรวม: 138.00 บาท', message)
        self.assertIn('คืนเงินแล้ว: 62.00 บาท', message)
        self.assertEqual(message, group.call_args.args[1])

    def test_initial_approval_stage_keeps_request_and_shows_daily_snapshot(self):
        claim = self.make_claim(actual=None, ledger=False, status=ClaimRequest.Status.PENDING_SUPERVISOR)
        message = self.summary(claim)
        self.assertIn('ยอดขอเบิก: 200.00 บาท', message)
        self.assertNotIn('ยอดขอสะสม', message)
        self.assertIn('รวมคำขอนี้: 450.00 บาท', message)
        self.assertNotIn('รับเงินแล้ว:', message)

    def test_excess_due_and_paid_but_unconfirmed_are_distinct(self):
        claim = self.make_claim(requested='100', actual='150', status=ClaimRequest.Status.PENDING_EXCESS_APPROVAL)
        message = self.summary(claim)
        self.assertIn('รับเงินแล้ว: 100.00 บาท', message)
        self.assertIn('ยอดใช้จริงรวม: 150.00 บาท', message)
        self.assertIn('ต้องจ่ายเพิ่ม: 50.00 บาท', message)
        self.assertNotIn('จ่ายเพิ่มแล้ว:', message)
        self.assertNotIn('ยอดขอสะสม', message)
        self.payment(claim, ClaimPayment.Kind.SUPPLEMENTAL, '50')
        claim.status = ClaimRequest.Status.PAID
        message = self.summary(claim)
        self.assertIn('จ่ายเงินแล้วรวม: 150.00 บาท', message)
        self.assertIn('จ่ายเพิ่มแล้ว: 50.00 บาท', message)
        self.assertNotIn('รับเงินแล้ว:', message)
        self.assertNotIn('คืนเงินแล้ว:', message)
        self.assertNotIn('ต้องจ่ายเพิ่ม:', message)

    def test_100_45_then_correct_50_and_close_matches_user_example(self):
        claim = self.make_claim(requested='100', actual='45', status=ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW)
        message = self.summary(claim)
        self.assertIn('รับเงินแล้ว: 100.00 บาท', message)
        self.assertIn('ยอดใช้จริงรวม: 45.00 บาท', message)
        self.assertIn('ต้องคืนเงิน: 55.00 บาท', message)
        self.assertIn('Postage: 45.00 บาท | Budget', message)
        self.assertEqual(message.count('100.00 บาท'), 1)
        self.client.force_login(self.accountant)
        with patch('apps.claims.views.send_telegram_notification') as bot, patch('apps.claims.views.send_telegram_group_notification') as group:
            self.client.post(reverse('claim_clearance_correct', args=[claim.pk]), {f'corrected_actual_{claim.items.get().pk}': '50'})
        claim.refresh_from_db()
        self.assertEqual(claim.actual_total, Decimal('50'))
        message = bot.call_args.args[2]
        self.assertIn('จาก 45.00 เป็น 50.00', message)
        self.assertIn('รับเงินแล้ว: 100.00 บาท', message)
        self.assertIn('ยอดใช้จริงรวม: 50.00 บาท', message)
        self.assertIn('ต้องคืนเงิน: 50.00 บาท', message)
        self.assertEqual(message, group.call_args.args[1])
        with patch('apps.claims.views.send_telegram_notification') as bot:
            self.client.post(reverse('claim_complete', args=[claim.pk]), {'refund_confirmed': '1'})
        message = bot.call_args.args[2]
        self.assertIn('รับเงินแล้ว: 100.00 บาท', message)
        self.assertIn('ยอดใช้จริงรวม: 50.00 บาท', message)
        self.assertIn('คืนเงินแล้ว: 50.00 บาท', message)
        self.assertNotIn('ต้องคืนเงิน:', message)
        self.assertNotIn('ยอดขอสะสม', message)
        self.assertNotIn('(ยอดขอ', message)
        self.assertEqual(message.count('100.00 บาท'), 1)
