from io import BytesIO
from html import unescape
import re
from datetime import date
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from PIL import Image
from django.contrib.auth import login
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.return_target import GATE_TARGET, SSO_TARGET, safe_target
from apps.claims.models import ClaimRequest
from apps.core.models import SystemSettings


class AuthenticationReturnTargetTests(TestCase):
    def setUp(self):
        self.config, _ = SystemSettings.objects.get_or_create(pk=1)
        self.config.require_signature_on_first_login = False
        self.config.ciam_sso_enabled = True
        self.config.save()
        self.user = User.objects.create_user(username='returnuser', password='InitialPass12@')
        self.target = '/claims/123/?tab=attachments&view=detail'

    def begin_sso(self, target=None, endpoint='sso_login_redirect'):
        response = self.client.get(reverse(endpoint), {'next': target or self.target})
        self.assertEqual(response.status_code, 302)
        state = parse_qs(urlsplit(response.url).query)['state'][0]
        self.assertEqual(self.client.session[SSO_TARGET], {'state': state, 'path': target or self.target})
        return state

    def test_login_page_carries_request_target_into_sso_button(self):
        response = self.client.get(reverse('login'), {'next': self.target})
        link = re.search(r'href="(/auth/sso/login/[^\"]*)"', response.content.decode())
        self.assertIsNotNone(link)
        self.assertEqual(parse_qs(urlsplit(unescape(link.group(1))).query)['next'], [self.target])

    def test_sso_callback_returns_to_original_request_and_consumes_target(self):
        claim = ClaimRequest.objects.create(pk=123, requester=self.user, request_date=date.today(), purpose='Link test')
        state = self.begin_sso()

        def exchange(request, *args):
            login(request, self.user)
            return self.user, None

        with patch('apps.accounts.views.exchange_code_and_login', side_effect=exchange):
            response = self.client.get(reverse('sso_callback'), {'code': 'test-code', 'state': state})
        self.assertRedirects(response, self.target, fetch_redirect_response=False)
        self.assertNotIn(SSO_TARGET, self.client.session)
        detail = self.client.get(self.target)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.context['claim'].pk, claim.pk)
        # Destination still uses existing claim permissions; it is not a bypass.
        stranger = User.objects.create_user(username='unrelateduser', password='InitialPass12@')
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(self.target).status_code, 404)

    def test_callback_error_keeps_target_for_retry(self):
        state = self.begin_sso()
        with patch('apps.accounts.views.exchange_code_and_login', return_value=(None, 'SSO unavailable')):
            response = self.client.get(reverse('sso_callback'), {'code': 'test-code', 'state': state})
        self.assertEqual(response.url, reverse('login') + '?' + urlencode({'next': self.target}))
        self.assertNotIn(SSO_TARGET, self.client.session)

    def test_wrong_state_does_not_reuse_target(self):
        self.begin_sso()
        with patch('apps.accounts.views.exchange_code_and_login', return_value=(None, 'Invalid state')):
            response = self.client.get(reverse('sso_callback'), {'code': 'test-code', 'state': 'wrong'})
        self.assertRedirects(response, reverse('login'), fetch_redirect_response=False)
        self.assertNotIn(SSO_TARGET, self.client.session)

    def test_new_portal_login_without_next_discards_older_target(self):
        self.begin_sso()
        self.client.get(reverse('sso_start'))
        self.assertEqual(self.client.session[SSO_TARGET]['path'], '')
        state = self.client.session['sso_state']
        with patch('apps.accounts.views.exchange_code_and_login', return_value=(self.user, None)):
            response = self.client.get(reverse('sso_callback'), {'code': 'test-code', 'state': state})
        self.assertRedirects(response, reverse('dashboard'), fetch_redirect_response=False)

    def test_direct_local_and_ad_login_preserve_target(self):
        for mode in ('local', 'ad'):
            with self.subTest(mode=mode):
                self.client.logout()
                self.user.is_ad_auth = mode == 'ad'
                self.user.save()
                with patch('apps.accounts.views.verify_bot_defense', return_value=(True, '', 200)), patch(
                    'apps.accounts.views.verify_ad_credentials', return_value=True
                ):
                    response = self.client.post(reverse('login'), {
                        'username': self.user.username, 'password': 'InitialPass12@',
                        'login_mode': mode, 'next': self.target,
                    })
                self.assertRedirects(response, self.target, fetch_redirect_response=False)

    def test_rejects_external_and_authentication_loop_targets(self):
        request = RequestFactory().get('/', secure=True)
        for target in (
            'https://evil.example/claims/123/', '//evil.example/claims/123/',
            '/\\evil.example/', 'http://testserver/claims/123/',
            '/claims/123/\nLocation:evil', reverse('login'), reverse('logout'),
            reverse('sso_callback_api'), reverse('sso_start_no_slash'),
            reverse('forced_password_change'),
        ):
            with self.subTest(target=target):
                self.assertEqual(safe_target(request, target), '')
        self.assertEqual(safe_target(request, 'https://testserver' + self.target), self.target)

    def test_external_target_is_not_exposed_in_login_button_or_saved(self):
        response = self.client.get(reverse('login'), {'next': 'https://evil.example/'})
        self.assertNotContains(response, 'evil.example')
        self.client.get(reverse('sso_login_redirect'), {'next': 'https://evil.example/'})
        self.assertEqual(self.client.session[SSO_TARGET]['path'], '')

    def test_password_gate_keeps_claim_destination_until_password_is_changed(self):
        self.user.must_change_password = True
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.get(self.target)
        self.assertRedirects(response, reverse('forced_password_change'), fetch_redirect_response=False)
        self.assertEqual(self.client.session[GATE_TARGET], self.target)
        response = self.client.post(reverse('forced_password_change'), {
            'old_password': 'InitialPass12@', 'new_password': 'NewSecret2026!',
            'confirm_password': 'NewSecret2026!',
        })
        self.assertRedirects(response, self.target, fetch_redirect_response=False)
        self.assertNotIn(GATE_TARGET, self.client.session)

    def test_password_then_signature_gate_still_returns_to_claim(self):
        self.config.require_signature_on_first_login = True
        self.config.save()
        self.user.must_change_password = True
        self.user.save()
        self.client.force_login(self.user)
        self.client.get(self.target)
        response = self.client.post(reverse('forced_password_change'), {
            'old_password': 'InitialPass12@', 'new_password': 'NewSecret2026!',
            'confirm_password': 'NewSecret2026!',
        })
        self.assertRedirects(response, reverse('signature_setup'), fetch_redirect_response=False)
        self.assertEqual(self.client.session[GATE_TARGET], self.target)
        image = BytesIO()
        Image.new('RGB', (2, 2)).save(image, format='PNG')
        with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            response = self.client.post(reverse('signature_setup'), {
                'action': 'signature',
                'signature_upload': SimpleUploadedFile('signature.png', image.getvalue(), content_type='image/png'),
            })
        self.assertRedirects(response, self.target, fetch_redirect_response=False)
        self.assertNotIn(GATE_TARGET, self.client.session)
