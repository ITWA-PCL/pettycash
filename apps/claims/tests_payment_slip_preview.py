from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from .models import ClaimApproval, ClaimPayment, ClaimRequest


class PaymentSlipPreviewTests(TestCase):
    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_context = self.settings(MEDIA_ROOT=self.media.name)
        self.settings_context.enable()
        self.addCleanup(self.settings_context.disable)
        for name in ('requester', 'accountant', 'manager', 'cfo', 'supervisor', 'other'):
            user = User.objects.create_user(username=f'slip-preview-{name}')
            user.signature.name = 'signatures/test/preview.png'
            user.save()
            setattr(self, name, user)
        self.admin = User.objects.create_superuser(username='slip-preview-admin')
        self.admin.signature.name = 'signatures/test/preview.png'
        self.admin.save()
        self.claim = ClaimRequest.objects.create(requester=self.requester, accountant=self.accountant,
            accounting_manager=self.manager, cfo=self.cfo, supervisor=self.supervisor,
            request_type='PETTY_CASH', status=ClaimRequest.Status.COMPLETED,
            work_location='HQ', purpose='Payment slip preview', requested_total='100', actual_total='100')
        for actor in (self.manager, self.supervisor):
            self.claim.approval_records.create(actor=actor, action=ClaimApproval.Action.APPROVED,
                status_before=ClaimRequest.Status.PENDING_ACCOUNTING, status_after=ClaimRequest.Status.PENDING_PAYMENT)
        self.client.force_login(self.accountant)

    def payment(self, name='receipt.png', kind=ClaimPayment.Kind.INITIAL, **kwargs):
        return self.claim.payments.create(kind=kind, method=ClaimPayment.Method.TRANSFER,
            amount='100', paid_by=self.accountant,
            slip=SimpleUploadedFile(name, b'%PDF-1.4 test' if name.lower().endswith('.pdf') else b'\x89PNG\r\n\x1a\npreview', content_type='application/octet-stream'),
            slip_original_name=name, **kwargs)

    def url(self, payment, preview=True, claim=None):
        return reverse('claim_payment_slip_preview' if preview else 'claim_payment_slip_download',
            args=[(claim or self.claim).pk, payment.pk])

    def test_image_and_pdf_preview_are_inline_same_origin_and_download_is_attachment(self):
        for name, content_type in (('receipt.png', 'image/png'), ('receipt.jpg', 'image/jpeg'), ('receipt.PDF', 'application/pdf')):
            payment = self.payment(name)
            for preview in (True, False):
                response = self.client.get(self.url(payment, preview))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], content_type)
                self.assertTrue(response['Content-Disposition'].startswith('inline' if preview else 'attachment'))
                self.assertIn('no-store', response['Cache-Control'])
                self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
                if preview:
                    self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')
                self.assertTrue(b''.join(response.streaming_content))

    def test_authorized_roles_can_preview_and_head_cannot_even_if_claim_is_visible(self):
        payment = self.payment()
        for actor in (self.requester, self.accountant, self.manager, self.cfo, self.admin):
            self.client.force_login(actor)
            response = self.client.get(self.url(payment))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(b''.join(response.streaming_content))
        for actor in (self.supervisor, self.other):
            self.client.force_login(actor)
            for preview in (True, False):
                self.assertEqual(self.client.get(self.url(payment, preview)).status_code, 404)
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse('claim_detail', args=[self.claim.pk])).status_code, 200)

    def test_cross_claim_payment_id_and_missing_or_cash_slips_are_rejected(self):
        other_claim = ClaimRequest.objects.create(requester=self.other, work_location='HQ', purpose='Unrelated')
        payment = self.payment()
        payment.claim_request = other_claim
        payment.save(update_fields=['claim_request'])
        self.assertEqual(self.client.get(self.url(payment)).status_code, 404)
        missing = self.claim.payments.create(kind='INITIAL', method='TRANSFER', amount='100', paid_by=self.accountant)
        self.assertEqual(self.client.get(self.url(missing)).status_code, 404)
        cash = self.payment()
        cash.method = 'CASH'
        cash.save(update_fields=['method'])
        self.assertEqual(self.client.get(self.url(cash)).status_code, 404)

    def test_missing_storage_file_is_404_not_server_error(self):
        payment = self.payment()
        payment.slip.name = 'payment-slips/nonexistent.pdf'
        payment.slip_original_name = 'nonexistent.pdf'
        payment.save(update_fields=['slip', 'slip_original_name'])
        for preview in (True, False):
            self.assertEqual(self.client.get(self.url(payment, preview)).status_code, 404)

    def test_preview_rejects_active_content_and_requires_login(self):
        payment = self.payment('unsafe.html')
        self.assertEqual(self.client.get(self.url(payment)).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url(payment)).status_code, 302)

    def test_history_uses_shared_preview_controls_and_bold_payment_method(self):
        payment = self.payment('ใบโอน.pdf', kind=ClaimPayment.Kind.SUPPLEMENTAL)
        self.claim.payments.create(kind='INITIAL', method='CASH', amount='100', paid_by=self.accountant)
        response = self.client.get(reverse('claim_detail', args=[self.claim.pk]))
        self.assertContains(response, 'class="payment-method"')
        self.assertContains(response, 'font-weight:700;">โอนเงิน</strong>')
        self.assertContains(response, 'font-weight:700;">เงินสด</strong>')
        self.assertContains(response, f'data-preview-url="{self.url(payment)}"')
        self.assertContains(response, f'href="{self.url(payment, False)}"')
        self.assertContains(response, 'data-file-name="ใบโอน.pdf"')
        self.assertContains(response, 'id="attachment-preview-panel"', count=1)
        self.client.force_login(self.supervisor)
        response = self.client.get(reverse('claim_detail', args=[self.claim.pk]))
        self.assertNotContains(response, 'ประวัติการจ่ายเงิน')
        self.assertNotContains(response, self.url(payment))
