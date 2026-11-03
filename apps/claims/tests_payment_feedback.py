from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from .models import ClaimRequest


@override_settings(TELEGRAM_BOT_TOKEN='')
class PaymentFeedbackTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username='slip-feedback-requester')
        self.accountant = User.objects.create_user(username='slip-feedback-accountant')
        self.accountant.signature.name = 'signatures/test/slip.png'
        self.accountant.save()
        self.client.force_login(self.accountant)

    def test_feedback_and_server_guard_apply_to_all_payment_types(self):
        for request_type, actual in (('PETTY_CASH', None), ('ADVANCE', None), ('ADVANCE', Decimal('250'))):
            claim = ClaimRequest.objects.create(requester=self.requester, accountant=self.accountant,
                status=ClaimRequest.Status.PENDING_PAYMENT, request_type=request_type,
                purpose='Slip feedback', work_location='HQ', requested_total='200.00', actual_total=actual)
            response = self.client.get(reverse('claim_detail', args=[claim.pk]))
            self.assertContains(response, 'id="payment-slip-error"')
            self.assertContains(response, 'กรุณาแนบสลิปการโอนก่อนบันทึกจ่ายเงิน')
            self.assertContains(response, 'aria-describedby="payment-slip-error"')
            response = self.client.post(reverse('claim_payment', args=[claim.pk]), {'payment_method': 'TRANSFER'}, follow=True)
            self.assertContains(response, 'การโอนเงินต้องแนบสลิปก่อนบันทึกจ่ายเงิน')
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
            self.assertFalse(claim.payments.exists())
