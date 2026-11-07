import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.claims.models import ClaimRequest
from apps.core.system_settings import get_system_settings
from .models import User


class PrivateSignatureTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        setting = override_settings(MEDIA_ROOT=self.directory.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.user = User.objects.create_user(username='signature-owner')
        self.user.signature.save('owner.png', SimpleUploadedFile('owner.png', b'private-signature'))
        self.other = User.objects.create_user(username='signature-other')
        self.other.signature.save('other.png', SimpleUploadedFile('other.png', b'other-signature'))
        self.config = get_system_settings()
        self.config.require_signature_on_first_login = False
        self.config.claim_print_enabled = True
        self.config.save()
        self.claim = ClaimRequest.objects.create(requester=self.user, purpose='Print', status=ClaimRequest.Status.COMPLETED)
        self.client.force_login(self.user)

    def image_url(self, user=None):
        return reverse('claim_signature_image', args=[self.claim.pk, (user or self.user).pk])

    def test_own_signature_and_completed_claim_image_are_private(self):
        for url in [reverse('signature_image'), self.image_url()]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'image/png')
            self.assertEqual(response['Cache-Control'], 'private, no-store')
            self.assertEqual(b''.join(response.streaming_content), b'private-signature')

    def test_print_and_profile_use_protected_urls(self):
        page = self.client.get(reverse('claim_print', args=[self.claim.pk]))
        self.assertContains(page, self.image_url())
        self.assertNotContains(page, self.user.signature.url)
        page = self.client.get(reverse('signature_setup'))
        self.assertContains(page, reverse('signature_image'))
        self.assertNotContains(page, self.user.signature.url)

    def test_anonymous_cannot_get_images(self):
        self.client.logout()
        for url in [reverse('signature_image'), self.image_url()]:
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_unrelated_user_cannot_view_claim_signature(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.image_url()).status_code, 404)

    def test_visible_claim_does_not_grant_access_to_unrelated_signature(self):
        self.assertEqual(self.client.get(self.image_url(self.other)).status_code, 404)

    def test_disabled_print_and_unfinished_claim_hide_signatures(self):
        self.config.claim_print_enabled = False
        self.config.save()
        self.assertEqual(self.client.get(self.image_url()).status_code, 404)
        self.config.claim_print_enabled = True
        self.config.save()
        self.claim.status = ClaimRequest.Status.PENDING_PAYMENT
        self.claim.save()
        self.assertEqual(self.client.get(self.image_url()).status_code, 404)

    def test_missing_file_is_not_a_server_error(self):
        self.user.signature.storage.delete(self.user.signature.name)
        self.assertEqual(self.client.get(reverse('signature_image')).status_code, 404)
        self.assertEqual(self.client.get(self.image_url()).status_code, 404)
