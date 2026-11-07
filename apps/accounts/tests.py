from django.core.exceptions import ValidationError
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.contrib.auth.models import Group
from unittest.mock import patch
import requests

from .ad_auth import ADGatewayError, verify_ad_credentials
from .forms import UserManagementForm
from .models import User
from .roles import ACCOUNTANT, ROLE_NAMES
from .roles import SUPER_ADMIN


class UserModelTests(TestCase):
    def test_theme_preference_defaults_to_light(self):
        user = User.objects.create_user(username="light-theme", password="safe-test-password")

        self.assertEqual(user.theme_preference, User.ThemePreference.LIGHT)

    def test_user_can_save_own_theme_preference(self):
        user = User.objects.create_user(username="theme-owner", password="safe-test-password")
        user.signature.name = "signatures/test/theme-owner.png"
        user.save(update_fields=["signature"])
        self.client.force_login(user)

        response = self.client.post(reverse("theme_preference"), {"mode": User.ThemePreference.DARK})

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.theme_preference, User.ThemePreference.DARK)

    def test_theme_preference_rejects_invalid_value(self):
        user = User.objects.create_user(username="invalid-theme", password="safe-test-password")
        user.signature.name = "signatures/test/invalid-theme.png"
        user.save(update_fields=["signature"])
        self.client.force_login(user)

        response = self.client.post(reverse("theme_preference"), {"mode": "SYSTEM"})

        self.assertEqual(response.status_code, 400)

    def test_user_cannot_be_own_supervisor(self):
        user = User.objects.create_user(username="requester", password="safe-test-password")
        user.supervisor = user

        with self.assertRaises(ValidationError):
            user.full_clean()

    def test_signature_flag_is_false_without_file(self):
        user = User.objects.create_user(username="without-signature", password="safe-test-password")

        self.assertFalse(user.has_signature)

    def test_user_without_signature_is_redirected_to_signature_setup(self):
        user = User.objects.create_user(username="new-user", password="safe-test-password")
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard"))

        self.assertRedirects(response, reverse("signature_setup"))

    def test_signature_requirement_can_be_disabled_without_disabling_profile(self):
        from apps.core.system_settings import get_system_settings

        user = User.objects.create_user(username="optional-signature", password="safe-test-password")
        configuration = get_system_settings()
        configuration.require_signature_on_first_login = False
        configuration.save(update_fields=["require_signature_on_first_login"])
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "ตั้งค่าลายเซ็นก่อนเริ่มใช้งาน")
        self.assertEqual(self.client.get(reverse("signature_setup")).status_code, 200)

        configuration.require_signature_on_first_login = True
        configuration.save(update_fields=["require_signature_on_first_login"])
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("signature_setup"))

    def test_signature_page_shows_existing_signature(self):
        user = User.objects.create_user(username="signed-user", password="safe-test-password")
        user.signature.name = "signatures/test/signature.png"
        user.save(update_fields=["signature"])
        self.client.force_login(user)

        response = self.client.get(reverse("signature_setup"))

        self.assertContains(response, reverse('signature_image'))
        self.assertNotContains(response, user.signature.url)

    def test_profile_form_requires_confirmation_before_submission(self):
        user = User.objects.create_user(username="profile-confirmation", password="safe-test-password")
        user.signature.name = "signatures/test/signature.png"
        user.save(update_fields=["signature"])
        self.client.force_login(user)

        response = self.client.get(reverse("signature_setup"))

        self.assertContains(response, 'data-confirm="ยืนยันบันทึกข้อมูลโปรไฟล์?"')
        self.assertContains(response, 'data-confirm="ยืนยันบันทึกลายเซ็นนี้? ระบบจะใช้กับเอกสารคำขอในอนาคต"')

    def test_user_can_update_own_profile_details(self):
        user = User.objects.create_user(username="profile-user", password="safe-test-password")
        user.signature.name = "signatures/test/signature.png"
        user.save(update_fields=["signature"])
        self.client.force_login(user)

        response = self.client.post(reverse("signature_setup"), {
            "action": "profile", "first_name": "สมชาย", "last_name": "ใจดี",
            "email": "somchai@example.com", "telegram_user_id": "123456789",
        })

        self.assertRedirects(response, reverse("signature_setup"))
        user.refresh_from_db()
        self.assertEqual(user.email, "somchai@example.com")
        self.assertEqual(user.telegram_user_id, "123456789")

    def test_local_user_can_change_own_password(self):
        user = User.objects.create_user(username="password-user", password="old-safe-password")
        user.signature.name = "signatures/test/signature.png"
        user.save(update_fields=["signature"])
        self.client.force_login(user)

        response = self.client.post(reverse("signature_setup"), {
            "action": "password", "password": "new-safe-password-2026",
        })

        self.assertRedirects(response, reverse("signature_setup"))
        user.refresh_from_db()
        self.assertTrue(user.check_password("new-safe-password-2026"))

    def test_ad_user_does_not_see_password_change_form(self):
        user = User.objects.create_user(username="ad-profile-user", password="safe-test-password", is_ad_auth=True)
        user.signature.name = "signatures/test/signature.png"
        user.save(update_fields=["signature"])
        self.client.force_login(user)

        response = self.client.get(reverse("signature_setup"))

        self.assertNotContains(response, "เปลี่ยนรหัสผ่าน")

    def test_local_login_still_works(self):
        user = User.objects.create_user(username="local-login", password="safe-test-password")
        user.signature.name = "signatures/test/signature.png"
        user.save(update_fields=["signature"])

        response = self.client.post(reverse("login"), {"username": "local-login", "password": "safe-test-password", "login_mode": "local"})

        self.assertRedirects(response, reverse("dashboard"))

    def test_ad_login_requires_enabled_local_user(self):
        response = self.client.post(reverse("login"), {"username": "not-registered-in-app", "password": "secret", "login_mode": "ad"})

        self.assertContains(response, "บัญชีนี้ไม่ได้รับอนุญาตให้ใช้ Active Directory")

    def test_ad_only_user_can_be_created_without_local_password(self):
        form = UserManagementForm(
            {"username": "ad-only-user", "work_location": User.WorkLocation.HEAD_OFFICE, "is_ad_auth": "on"},
            creating=True,
        )

        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertTrue(user.is_ad_auth)
        self.assertFalse(user.has_usable_password())

    def test_local_user_requires_a_password_when_created(self):
        form = UserManagementForm(
            {"username": "local-only-user", "work_location": User.WorkLocation.HEAD_OFFICE}, creating=True
        )

        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    @override_settings(AD_GATEWAY_URL="http://ad-gateway.example/login", AD_APP_ID="app", AD_SECRET_KEY="secret")
    @patch("apps.accounts.ad_auth.requests.post")
    def test_ad_gateway_http_422_has_safe_diagnostic(self, mock_post):
        response = requests.Response()
        response.status_code = 422
        mock_post.return_value = response

        with self.assertRaisesMessage(ADGatewayError, "HTTP 422"):
            verify_ad_credentials("ad-user", "password")

    @override_settings(AD_GATEWAY_URL="http://ad-gateway.example/login", AD_APP_ID="app", AD_SECRET_KEY="secret")
    @patch("apps.accounts.ad_auth.requests.post")
    def test_ad_gateway_expired_response_has_timestamp_diagnostic(self, mock_post):
        response = requests.Response()
        response.status_code = 403
        response._content = b'{"status":"error","message":"Expired"}'
        response.headers["Content-Type"] = "application/json"
        mock_post.return_value = response

        with self.assertRaisesMessage(ADGatewayError, "Timestamp"):
            verify_ad_credentials("ad-user", "password")

    @override_settings(AD_GATEWAY_URL="http://ad-gateway.example/login", AD_APP_ID="app", AD_SECRET_KEY="secret")
    @patch("apps.accounts.ad_auth.requests.post")
    def test_ad_gateway_secret_mismatch_has_configuration_diagnostic(self, mock_post):
        response = requests.Response()
        response.status_code = 403
        response._content = b'{"status":"error","message":"Unauthorized app: secret mismatch"}'
        response.headers["Content-Type"] = "application/json"
        mock_post.return_value = response

        with self.assertRaisesMessage(ADGatewayError, "AD Secret Key ในหน้าตั้งค่าระบบ"):
            verify_ad_credentials("ad-user", "password")

    def test_user_can_have_multiple_business_roles(self):
        user = User.objects.create_user(username="accounting-user", password="safe-test-password")
        user.groups.add(Group.objects.get(name=ACCOUNTANT))

        self.assertTrue(user.has_role(ACCOUNTANT))
        self.assertFalse(user.has_role("ผู้บริหาร"))
        self.assertEqual(set(ROLE_NAMES), set(Group.objects.filter(name__in=ROLE_NAMES).values_list("name", flat=True)))

    def test_only_super_admin_can_open_user_management(self):
        ordinary_user = User.objects.create_user(username="ordinary", password="safe-test-password")
        ordinary_user.signature.name = "signatures/test/signature.png"
        ordinary_user.save(update_fields=["signature"])
        self.client.force_login(ordinary_user)
        response = self.client.get(reverse("user_list"))
        self.assertEqual(response.status_code, 302)

        ordinary_user.groups.add(Group.objects.get(name=SUPER_ADMIN))
        response = self.client.get(reverse("user_list"))
        self.assertEqual(response.status_code, 200)

    def test_super_admin_can_toggle_and_delete_other_user(self):
        admin = User.objects.create_user(username="user-admin", password="safe-test-password")
        admin.groups.add(Group.objects.get(name=SUPER_ADMIN))
        admin.signature.name = "signatures/test/user-admin.png"
        admin.save(update_fields=["signature"])
        managed_user = User.objects.create_user(username="managed-user", password="safe-test-password")
        self.client.force_login(admin)

        response = self.client.post(reverse("user_toggle_active", args=[managed_user.pk]))
        self.assertRedirects(response, reverse("user_list"))
        managed_user.refresh_from_db()
        self.assertFalse(managed_user.is_active)

        response = self.client.post(reverse("user_toggle_active", args=[managed_user.pk]))
        self.assertRedirects(response, reverse("user_list"))
        managed_user.refresh_from_db()
        self.assertTrue(managed_user.is_active)

        response = self.client.post(reverse("user_delete", args=[managed_user.pk]))
        self.assertRedirects(response, reverse("user_list"))
        self.assertFalse(User.objects.filter(pk=managed_user.pk).exists())

    def test_user_delete_can_return_a_history_replacement_url(self):
        admin = User.objects.create_user(username="history-admin", password="safe-test-password")
        admin.groups.add(Group.objects.get(name=SUPER_ADMIN))
        admin.signature.name = "signatures/test/history-admin.png"
        admin.save(update_fields=["signature"])
        managed_user = User.objects.create_user(username="history-target", password="safe-test-password")
        self.client.force_login(admin)

        response = self.client.post(
            reverse("user_delete", args=[managed_user.pk]),
            HTTP_X_HISTORY_REPLACE="1",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"redirect": reverse("user_list")})
        self.assertFalse(User.objects.filter(pk=managed_user.pk).exists())

    def test_super_admin_cannot_disable_or_delete_self(self):
        admin = User.objects.create_user(username="self-admin", password="safe-test-password")
        admin.groups.add(Group.objects.get(name=SUPER_ADMIN))
        admin.signature.name = "signatures/test/self-admin.png"
        admin.save(update_fields=["signature"])
        self.client.force_login(admin)

        self.client.post(reverse("user_toggle_active", args=[admin.pk]))
        admin.refresh_from_db()
        self.assertTrue(admin.is_active)

        self.client.post(reverse("user_delete", args=[admin.pk]))
        self.assertTrue(User.objects.filter(pk=admin.pk).exists())

    @override_settings(TELEGRAM_BOT_TOKEN="test-token")
    @patch("apps.accounts.views.send_telegram_message", return_value=True)
    def test_super_admin_can_send_telegram_test_message(self, send_message):
        admin = User.objects.create_user(username="admin-telegram", password="safe-test-password")
        admin.groups.add(Group.objects.get(name=SUPER_ADMIN))
        admin.signature.name = "signatures/test/admin-telegram.png"
        admin.save(update_fields=["signature"])
        self.client.force_login(admin)

        response = self.client.post(reverse("telegram_test_message"), {"telegram_user_id": "123456789"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(send_message.call_args.args[0], "123456789")
