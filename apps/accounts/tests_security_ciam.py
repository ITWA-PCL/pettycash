import json
import time
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth import authenticate
from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.http import HttpRequest
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.bot_defense import verify_bot_defense, SpokeRateLimiter
from apps.accounts.models import User
from apps.accounts.password_policy import is_password_expired, validate_password_complexity
from apps.accounts.roles import SUPER_ADMIN
from apps.core.ciam_agent import execute_local_command, fetch_local_accounts
from apps.core.models import AdminAuditLog, SystemSettings, TransactionLog


class PasswordComplexityPolicyTests(TestCase):
    def test_length_bounds(self):
        self.assertIn("ระหว่าง 8 ถึง 16", validate_password_complexity("Short1!"))
        self.assertIn("ระหว่าง 8 ถึง 16", validate_password_complexity("ThisPasswordIsTooLong12345!"))

    def test_uppercase_required(self):
        self.assertIn("พิมพ์ใหญ่", validate_password_complexity("lowercase1@"))

    def test_lowercase_required(self):
        self.assertIn("พิมพ์เล็ก", validate_password_complexity("UPPERCASE1@"))

    def test_digit_required(self):
        self.assertIn("ตัวเลข", validate_password_complexity("NoDigitsPass!"))

    def test_special_char_required(self):
        self.assertIn("อักขระพิเศษ", validate_password_complexity("NoSpecialChar12"))

    def test_no_username_in_password(self):
        self.assertIn("ชื่อผู้ใช้", validate_password_complexity("Somchai123!", username="somchai"))
        self.assertIn("ชื่อผู้ใช้", validate_password_complexity("PassSomchai99$", username="SOMCHAI"))
        # Username under 3 characters is ignored
        self.assertIsNone(validate_password_complexity("StrongPass12#", username="ab"))

    def test_valid_password(self):
        self.assertIsNone(validate_password_complexity("Sec@2026Strong", username="user1"))


class PasswordRotationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.local_user = User.objects.create_user(
            username="localuser",
            password="InitialPass12@",
            is_ad_auth=False,
            auth_source=User.AuthSource.LOCAL,
            password_changed_at=timezone.now(),
        )
        self.ad_user = User.objects.create_user(
            username="aduser",
            password="InitialPass12@",
            is_ad_auth=True,
            auth_source=User.AuthSource.AD,
            password_changed_at=timezone.now() - timedelta(days=120),
        )

    def test_ad_account_never_expires_in_pettycash(self):
        is_expired, days_left = self.ad_user.check_password_expired()
        self.assertFalse(is_expired)
        self.assertEqual(days_left, 90)

    def test_local_account_not_expired_under_90_days(self):
        self.local_user.password_changed_at = timezone.now() - timedelta(days=30)
        self.local_user.save()
        is_expired, days_left = self.local_user.check_password_expired()
        self.assertFalse(is_expired)
        self.assertGreater(days_left, 50)

    def test_local_account_expired_over_90_days(self):
        self.local_user.password_changed_at = timezone.now() - timedelta(days=91)
        self.local_user.save()
        is_expired, days_left = self.local_user.check_password_expired()
        self.assertTrue(is_expired)
        self.assertEqual(days_left, 0)

    def test_must_change_password_flag(self):
        self.local_user.must_change_password = True
        self.local_user.save()
        is_expired, _ = self.local_user.check_password_expired()
        self.assertTrue(is_expired)

    def test_rotation_middleware_interception(self):
        # Expire user password
        self.local_user.password_changed_at = timezone.now() - timedelta(days=95)
        self.local_user.save()

        self.client.force_login(self.local_user)
        # Attempt to access dashboard
        res = self.client.get(reverse("dashboard"))
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.url, reverse("forced_password_change"))

    def test_forced_password_change_success(self):
        self.local_user.password_changed_at = timezone.now() - timedelta(days=95)
        self.local_user.save()

        self.client.force_login(self.local_user)
        res = self.client.post(reverse("forced_password_change"), {
            "old_password": "InitialPass12@",
            "new_password": "NewSecret2026!",
            "confirm_password": "NewSecret2026!",
        })
        self.assertEqual(res.status_code, 302)

        self.local_user.refresh_from_db()
        self.assertTrue(self.local_user.check_password("NewSecret2026!"))
        self.assertFalse(self.local_user.must_change_password)
        is_expired, _ = self.local_user.check_password_expired()
        self.assertFalse(is_expired)


class BotDefenseTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_honeypot_rejection(self):
        res = self.client.post(reverse("login"), {
            "username": "somchai",
            "password": "Password123!",
            "corporate_fax": "spambot-value",
        })
        self.assertEqual(res.status_code, 401)
        self.assertContains(res, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", status_code=401)

    def test_timing_velocity_rejection(self):
        now_ms = int(time.time() * 1000)
        # Submitted in 50ms (faster than 400ms human limit)
        res = self.client.post(reverse("login"), {
            "username": "somchai",
            "password": "Password123!",
            "form_render_ts": str(now_ms - 50),
        })
        self.assertEqual(res.status_code, 400)
        self.assertContains(res, "ตรวจพบความเร็วการส่งข้อมูลผิดปกติ", status_code=400)

    def test_rate_limiter_logic(self):
        limiter = SpokeRateLimiter(limit=3, window=60)
        test_ip = "198.51.100.22"
        self.assertTrue(limiter.is_allowed(test_ip)[0])
        self.assertTrue(limiter.is_allowed(test_ip)[0])
        self.assertTrue(limiter.is_allowed(test_ip)[0])
        allowed, retry_after = limiter.is_allowed(test_ip)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

        # Whitelist check
        self.assertTrue(limiter.is_allowed("157.173.219.153")[0])


class CentralIAMIntegrationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username="secadmin",
            password="AdminPass12#$",
            is_active=True,
            password_changed_at=timezone.now(),
            signature="signatures/admin.png",
        )

    def test_sso_config_public_endpoint(self):
        res = self.client.get(reverse("sso_config"))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("sso_enabled", data)
        self.assertIn("break_glass_active", data)

    def test_sso_authorize_url_generation(self):
        res = self.client.post(reverse("sso_authorize_url"))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("authorize_url", data)
        self.assertIn("code_verifier", data)
        self.assertIn("code_challenge", data["authorize_url"])

    def test_sso_break_glass_toggle(self):
        self.client.force_login(self.admin)
        res = self.client.post(reverse("sso_break_glass_toggle"))
        self.assertEqual(res.status_code, 302)
        settings_obj = SystemSettings.objects.get(pk=1)
        self.assertTrue(settings_obj.ciam_break_glass_active)

    def test_ciam_settings_api_get_and_put(self):
        self.client.force_login(self.admin)
        # GET
        res = self.client.get(reverse("ciam_settings_api"))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("ciam_client_secret_masked", data["settings"])

        # PUT
        res = self.client.post(reverse("ciam_settings_api"), {
            "ciam_base_url": "https://ciam.windowasia.com",
            "ciam_client_id": "pettycash-spoke-client",
            "ciam_client_secret": "test_client_secret_token",
            "ciam_sso_enabled": True,
        })
        self.assertEqual(res.status_code, 200)
        settings_obj = SystemSettings.objects.get(pk=1)
        self.assertEqual(settings_obj.ciam_client_secret, "test_client_secret_token")
        self.assertTrue(settings_obj.ciam_sso_enabled)

    @patch("requests.get")
    def test_ciam_test_connection(self, mock_get):
        self.client.force_login(self.admin)

        # Mock OIDC configuration
        mock_oidc = MagicMock()
        mock_oidc.status_code = 200
        mock_oidc.json.return_value = {
            "issuer": "https://ciam.windowasia.com",
            "jwks_uri": "https://ciam.windowasia.com/.well-known/jwks.json",
        }

        # Mock JWKS
        mock_jwks = MagicMock()
        mock_jwks.status_code = 200
        mock_jwks.json.return_value = {"keys": [{"kid": "ciam-key-2026-01"}]}

        mock_get.side_effect = [mock_oidc, mock_jwks]

        res = self.client.post(reverse("ciam_test_connection"))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "connected")
        self.assertEqual(data["key_id"], "ciam-key-2026-01")

    @patch("requests.post")
    @patch("apps.accounts.ciam_sso.get_jwks_client")
    def test_sso_exchange_code_validates_state_and_verifier(self, mock_jwks, mock_post):
        """Test SSO callback requires matching state and session verifier, binds ciam_sub."""
        from apps.accounts.ciam_sso import exchange_code_and_login
        from apps.core.system_settings import get_system_settings

        settings_obj = get_system_settings()
        settings_obj.ciam_sso_enabled = True
        settings_obj.ciam_break_glass_active = False
        settings_obj.save()

        request = HttpRequest()
        request.session = self.client.session
        request.META = {"REMOTE_ADDR": "127.0.0.1"}

        # 1. State mismatch test
        request.session["sso_state"] = "expected_state_123"
        request.session["sso_code_verifier"] = "test_verifier_abc"

        user, err = exchange_code_and_login(request, "auth_code", None, "http://localhost/callback", state="wrong_state")
        self.assertIsNone(user)
        self.assertIn("state", err.lower())

        # 2. Missing verifier in session
        request.session["sso_state"] = "valid_state"
        request.session.pop("sso_code_verifier", None)
        # sso_code_verifier is missing
        user, err = exchange_code_and_login(request, "auth_code", None, "http://localhost/callback", state="valid_state")
        self.assertIsNone(user)
        self.assertIn("pkce", err.lower())

        # 3. Successful exchange and ciam_sub binding
        request.session["sso_state"] = "valid_state"
        request.session["sso_code_verifier"] = "valid_verifier"

        # Mock token endpoint
        mock_token_resp = MagicMock()
        mock_token_resp.status_code = 200
        mock_token_resp.json.return_value = {"id_token": "mock.jwt.token"}
        mock_post.return_value = mock_token_resp

        # Mock JWKS & JWT Decode
        mock_client_instance = MagicMock()
        mock_jwks.return_value = mock_client_instance
        with patch("jwt.decode") as mock_jwt_decode, patch("django.contrib.auth.login"):
            mock_jwt_decode.return_value = {
                "sub": "ciam-uuid-9999",
                "preferred_username": "new_sso_user",
                "name": "SSO Test User",
                "email": "sso@windowasia.com",
            }
            user, err = exchange_code_and_login(request, "auth_code", None, "http://localhost/callback", state="valid_state")
            self.assertIsNone(err)
            self.assertIsNotNone(user)
            self.assertEqual(user.ciam_sub, "ciam-uuid-9999")
            self.assertEqual(user.username, "new_sso_user")

    @patch("requests.post")
    @patch("apps.accounts.ciam_sso.get_jwks_client")
    def test_sso_rejects_username_collision_with_different_sub(self, mock_jwks, mock_post):
        """Security: Reject SSO if existing local account has different ciam_sub."""
        from apps.accounts.ciam_sso import exchange_code_and_login
        from apps.core.system_settings import get_system_settings

        settings_obj = get_system_settings()
        settings_obj.ciam_sso_enabled = True
        settings_obj.ciam_break_glass_active = False
        settings_obj.save()

        # Existing user bound to sub-1111
        User.objects.create_user(
            username="conflicted_user",
            ciam_sub="sub-1111",
            password="ConflictedPass12#",
            is_active=True,
            password_changed_at=timezone.now(),
        )

        request = HttpRequest()
        request.session = self.client.session
        request.session["sso_state"] = "valid_state"
        request.session["sso_code_verifier"] = "valid_verifier"
        request.META = {"REMOTE_ADDR": "127.0.0.1"}

        mock_token_resp = MagicMock()
        mock_token_resp.status_code = 200
        mock_token_resp.json.return_value = {"id_token": "mock.jwt.token"}
        mock_post.return_value = mock_token_resp

        mock_client_instance = MagicMock()
        mock_jwks.return_value = mock_client_instance

        # Incoming CIAM token with same username but different sub (sub-2222)
        with patch("jwt.decode") as mock_jwt_decode, patch("django.contrib.auth.login"):
            mock_jwt_decode.return_value = {
                "sub": "sub-2222",
                "preferred_username": "conflicted_user",
                "name": "Different Employee",
                "email": "diff@windowasia.com",
            }
            user, err = exchange_code_and_login(request, "auth_code", None, "http://localhost/callback", state="valid_state")
            self.assertIsNone(user)
            self.assertIn("ผูกกับ subject identifier อื่น", err.lower())

    def test_extract_client_ip_strict_proxy_check(self):
        """Test proxy IP trust: only loopback or explicit TRUSTED_PROXIES can forward IP."""
        from apps.accounts.bot_defense import extract_client_ip

        req = HttpRequest()
        req.META = {
            "REMOTE_ADDR": "198.51.100.55",  # External untrusted IP
            "HTTP_X_FORWARDED_FOR": "10.0.0.1, 192.168.1.1",
        }
        # Must NOT trust X-Forwarded-For from untrusted remote addr
        self.assertEqual(extract_client_ip(req), "198.51.100.55")

        # Loopback proxy (127.0.0.1) IS trusted
        req.META["REMOTE_ADDR"] = "127.0.0.1"
        self.assertEqual(extract_client_ip(req), "10.0.0.1")

    def test_ciam_sync_now_security_post_only(self):
        """Security Issue 5: ciam_sync_now must reject GET and validate redirect url."""
        self.client.force_login(self.admin)

        # GET should be 405 Method Not Allowed
        get_res = self.client.get(reverse("ciam_sync_now"))
        self.assertEqual(get_res.status_code, 405)

        # POST with external redirect target should fallback to user_list
        with patch("apps.core.ciam_agent.run_agent_cycle") as mock_cycle:
            mock_cycle.return_value = ([], {}, {"status": "ok"})
            post_res = self.client.post(reverse("ciam_sync_now"), {"next": "https://evil-phishing.com/steal"})
            self.assertEqual(post_res.status_code, 302)
            self.assertEqual(post_res.url, reverse("user_list"))

    def test_ciam_directory_accounts_m2m_api(self):
        """Test GET /api/v1/directory/accounts/ with X-Management-API-Key."""
        from apps.core.system_settings import get_system_settings
        settings_obj = get_system_settings()
        settings_obj.ciam_m2m_key = "test_management_api_key_valid"
        settings_obj.save()

        # 1. Missing / wrong key
        res = self.client.get(reverse("ciam_directory_accounts_api"))
        self.assertEqual(res.status_code, 401)

        res = self.client.get(
            reverse("ciam_directory_accounts_api"),
            HTTP_X_MANAGEMENT_API_KEY="wrong_key",
        )
        self.assertEqual(res.status_code, 401)

        # 2. Correct key
        res = self.client.get(
            reverse("ciam_directory_accounts_api"),
            HTTP_X_MANAGEMENT_API_KEY="test_management_api_key_valid",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertGreaterEqual(data["count"], 1)
        self.assertIn("accounts", data)

    def test_sso_callback_route_alias(self):
        """Verify /auth/callback/ route is handled equivalently to /auth/sso/callback/."""
        res = self.client.get(reverse("sso_callback_alias"))
        # Should redirect to login when no code provided
        self.assertEqual(res.status_code, 302)
        self.assertIn("login", res.url)

class ModeCDirectoryAgentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="regular_user",
            password="TestPass123!@",
            is_active=True,
            password_changed_at=timezone.now(),
        )

    def test_execute_local_command_disable_user(self):
        success, msg = execute_local_command("DISABLE_USER", "regular_user", "1-Click Offboarding")
        self.assertTrue(success)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_execute_local_command_enable_user(self):
        self.user.is_active = False
        self.user.save()
        success, msg = execute_local_command("ENABLE_USER", "regular_user", "Reactivate")
        self.assertTrue(success)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_execute_local_command_provision_user(self):
        success, msg = execute_local_command("PROVISION_USER", "new_wa_employee", "CIAM Assignment")
        self.assertTrue(success)
        new_u = User.objects.get(username="new_wa_employee")
        self.assertTrue(new_u.is_active)
        self.assertTrue(new_u.is_ad_auth)

    def test_execute_local_command_protects_admin(self):
        success, msg = execute_local_command("DISABLE_USER", "admin", "Attempt to disable admin")
        self.assertFalse(success)
        self.assertIn("Protected account", msg)

    def test_fetch_local_accounts(self):
        accounts = fetch_local_accounts()
        self.assertGreater(len(accounts), 0)
        self.assertIn("username", accounts[0])
        self.assertIn("is_active", accounts[0])


class LocalPasswordPolicyHardeningTests(TestCase):
    INVALID_PASSWORD_CASES = (
        ("too_short", "Ab1!xyz", "ระหว่าง 8 ถึง 16"),
        ("too_long", "Abcdefgh12345678!", "ระหว่าง 8 ถึง 16"),
        ("missing_upper", "lowercase123!", "พิมพ์ใหญ่"),
        ("missing_lower", "UPPERCASE123!", "พิมพ์เล็ก"),
        ("missing_digit", "NoDigitsHere!", "ตัวเลข"),
        ("missing_special", "NoSpecial1234", "อักขระพิเศษ"),
        ("contains_username", "PolicyUserx99!", "ชื่อผู้ใช้"),
    )

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username="policyadmin",
            password="AdminStrong26!",
            is_active=True,
            is_staff=True,
            password_changed_at=timezone.now(),
            signature="signatures/policyadmin.png",
        )
        self.admin.groups.add(Group.objects.get(name=SUPER_ADMIN))

        self.target = User.objects.create_user(
            username="policyuser",
            password="InitialStrong26!",
            is_active=True,
            is_ad_auth=False,
            auth_source=User.AuthSource.LOCAL,
            password_changed_at=timezone.now(),
            signature="signatures/policyuser.png",
        )
        self.ad_admin = User.objects.create_superuser(
            username="adpolicyadmin",
            password=None,
            is_active=True,
            is_staff=True,
            is_ad_auth=True,
            auth_source=User.AuthSource.AD,
            password_changed_at=None,
            signature="signatures/adpolicyadmin.png",
        )
        self.ad_admin.groups.add(Group.objects.get(name=SUPER_ADMIN))

    def _assert_no_password_in_logs(self, *secrets_to_check):
        audit_blobs = [
            f"{entry.action} {entry.target} {entry.detail}"
            for entry in AdminAuditLog.objects.all()
        ]
        tx_blobs = [
            f"{entry.action} {entry.message} {entry.details}"
            for entry in TransactionLog.objects.all()
        ]
        admin_log_blobs = [entry.change_message for entry in LogEntry.objects.all()]
        all_logs = audit_blobs + tx_blobs + admin_log_blobs
        for secret in secrets_to_check:
            for blob in all_logs:
                self.assertNotIn(secret, blob)

    def test_expiry_boundary_day_89_and_day_90(self):
        day89 = timezone.now() - timedelta(days=89)
        self.target.password_changed_at = day89
        self.target.save(update_fields=["password_changed_at"])

        expired_89, left_89 = self.target.check_password_expired()
        self.assertFalse(expired_89)
        self.assertEqual(left_89, 1)

        self.client.force_login(self.target)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("notification_feed")).status_code, 200)

        day90 = timezone.now() - timedelta(days=90)
        self.target.password_changed_at = day90
        self.target.save(update_fields=["password_changed_at"])

        expired_90, left_90 = self.target.check_password_expired()
        self.assertTrue(expired_90)
        self.assertEqual(left_90, 0)

        web_res = self.client.get(reverse("dashboard"))
        self.assertEqual(web_res.status_code, 302)
        self.assertEqual(web_res.url, reverse("forced_password_change"))

        api_res = self.client.get(reverse("notification_feed"))
        self.assertEqual(api_res.status_code, 403)
        self.assertEqual(api_res.json()["error_code"], "PASSWORD_CHANGE_REQUIRED")
        self.assertEqual(api_res.json()["reason"], "password_expired")

    def test_legacy_account_without_password_changed_at_does_not_use_date_joined(self):
        legacy = User.objects.create_user(
            username="legacyuser",
            password="LegacyStrong26!",
            is_active=True,
            password_changed_at=None,
            signature="signatures/legacyuser.png",
        )
        legacy.refresh_from_db()
        self.assertIsNone(legacy.password_changed_at)

        expired, days_left = is_password_expired(legacy.password_changed_at, legacy.date_joined)
        self.assertTrue(expired)
        self.assertEqual(days_left, 0)
        self.assertEqual(legacy.check_password_expired(), (True, 0))

        self.client.force_login(legacy)
        web_res = self.client.get(reverse("dashboard"))
        self.assertEqual(web_res.status_code, 302)
        self.assertEqual(web_res.url, reverse("forced_password_change"))

        api_res = self.client.get(reverse("notification_feed"))
        self.assertEqual(api_res.status_code, 403)
        self.assertEqual(api_res.json()["error_code"], "PASSWORD_CHANGE_REQUIRED")
        self.assertEqual(api_res.json()["reason"], "password_changed_at_missing")

        # Must remain NULL until the user actually sets a new password
        legacy.refresh_from_db()
        self.assertIsNone(legacy.password_changed_at)

    def test_complexity_rules_enforced_across_all_channels(self):
        subject = User.objects.create_user(
            username="policyuserx",
            password="ValidCurrent26!",
            is_active=True,
            password_changed_at=timezone.now(),
            signature="signatures/policyuserx.png",
        )

        # 1. Profile password change & Forced password change
        for case_name, bad_pwd, expected_msg in self.INVALID_PASSWORD_CASES:
            with self.subTest(channel="profile", case=case_name):
                self.client.force_login(subject)
                res = self.client.post(reverse("signature_setup"), {
                    "action": "password",
                    "password": bad_pwd,
                })
                self.assertEqual(res.status_code, 200)
                self.assertIn(expected_msg, res.context["password_form"].errors["password"][0])
                subject.refresh_from_db()
                self.assertTrue(subject.check_password("ValidCurrent26!"))

            with self.subTest(channel="forced_change", case=case_name):
                res = self.client.post(reverse("forced_password_change"), {
                    "old_password": "ValidCurrent26!",
                    "new_password": bad_pwd,
                    "confirm_password": bad_pwd,
                })
                self.assertEqual(res.status_code, 200)
                self.assertIn(expected_msg, res.context["form"].errors["new_password"][0])
                subject.refresh_from_db()
                self.assertTrue(subject.check_password("ValidCurrent26!"))

            with self.subTest(channel="auth_validator", case=case_name):
                with self.assertRaises(ValidationError) as ctx:
                    validate_password(bad_pwd, subject)
                self.assertIn(expected_msg, str(ctx.exception))

        # 2. Admin UI create & reset, and Django Admin create & password change
        self.client.force_login(self.admin)
        for case_name, bad_pwd, expected_msg in self.INVALID_PASSWORD_CASES:
            create_bad_pwd = "PolicyUserx29!" if case_name == "contains_username" else bad_pwd
            with self.subTest(channel="ui_create", case=case_name):
                res = self.client.post(reverse("user_create"), {
                    "username": "policyuserx2",
                    "work_location": User.WorkLocation.HEAD_OFFICE,
                    "password": create_bad_pwd,
                    "is_active": "on",
                })
                self.assertEqual(res.status_code, 200)
                self.assertIn(expected_msg, res.context["form"].errors["password"][0])

            with self.subTest(channel="ui_edit_reset", case=case_name):
                res = self.client.post(reverse("user_edit", args=[subject.pk]), {
                    "username": subject.username,
                    "work_location": User.WorkLocation.HEAD_OFFICE,
                    "password": bad_pwd,
                    "is_active": "on",
                })
                self.assertEqual(res.status_code, 200)
                self.assertIn(expected_msg, res.context["form"].errors["password"][0])
                subject.refresh_from_db()
                self.assertTrue(subject.check_password("ValidCurrent26!"))

            with self.subTest(channel="django_admin_create", case=case_name):
                res = self.client.post(reverse("admin:accounts_user_add"), {
                    "username": "policyuserx2",
                    "usable_password": "true",
                    "password1": create_bad_pwd,
                    "password2": create_bad_pwd,
                })
                self.assertEqual(res.status_code, 200)
                self.assertIn(expected_msg, res.context["adminform"].form.errors["password2"][0])
                self.assertFalse(User.objects.filter(username="policyuserx2").exists())

            with self.subTest(channel="django_admin_password_change", case=case_name):
                res = self.client.post(reverse("admin:auth_user_password_change", args=[subject.pk]), {
                    "usable_password": "true",
                    "password1": bad_pwd,
                    "password2": bad_pwd,
                })
                self.assertEqual(res.status_code, 200)
                self.assertIn(expected_msg, res.context["adminForm"].form.errors["password2"][0])
                subject.refresh_from_db()
                self.assertTrue(subject.check_password("ValidCurrent26!"))

    def test_forbid_reusing_current_password_across_all_channels(self):
        old_ts = timezone.now() - timedelta(days=80)
        self.target.password_changed_at = old_ts
        self.target.save(update_fields=["password_changed_at"])

        # Profile password change
        self.client.force_login(self.target)
        res_profile = self.client.post(reverse("signature_setup"), {
            "action": "password",
            "password": "InitialStrong26!",
        })
        self.assertEqual(res_profile.status_code, 200)
        self.assertIn("ไม่ซ้ำกับรหัสผ่านเดิม", res_profile.context["password_form"].errors["password"][0])

        # Forced password change
        res_forced = self.client.post(reverse("forced_password_change"), {
            "old_password": "InitialStrong26!",
            "new_password": "InitialStrong26!",
            "confirm_password": "InitialStrong26!",
        })
        self.assertEqual(res_forced.status_code, 200)
        self.assertIn("ไม่ซ้ำกับรหัสผ่านเดิม", res_forced.context["form"].errors["new_password"][0])

        # Django validate_password
        with self.assertRaises(ValidationError) as ctx:
            validate_password("InitialStrong26!", self.target)
        self.assertIn("ไม่ซ้ำกับรหัสผ่านเดิม", str(ctx.exception))

        # Admin UI reset
        self.client.force_login(self.admin)
        res_ui_reset = self.client.post(reverse("user_edit", args=[self.target.pk]), {
            "username": self.target.username,
            "work_location": User.WorkLocation.HEAD_OFFICE,
            "password": "InitialStrong26!",
            "is_active": "on",
        })
        self.assertEqual(res_ui_reset.status_code, 200)
        self.assertIn("ไม่ซ้ำกับรหัสผ่านเดิม", res_ui_reset.context["form"].errors["password"][0])

        # Django Admin password change
        res_admin_pwd = self.client.post(reverse("admin:auth_user_password_change", args=[self.target.pk]), {
            "usable_password": "true",
            "password1": "InitialStrong26!",
            "password2": "InitialStrong26!",
        })
        self.assertEqual(res_admin_pwd.status_code, 200)
        self.assertIn("ไม่ซ้ำกับรหัสผ่านเดิม", res_admin_pwd.context["adminForm"].form.errors["password2"][0])

        # Ensure password_changed_at was NOT reset on rejected attempts
        self.target.refresh_from_db()
        self.assertEqual(self.target.password_changed_at, old_ts)

    def test_create_change_reset_ui_and_django_admin_with_session_invalidation(self):
        # 1. Create via Admin UI
        self.client.force_login(self.admin)
        res_create_ui = self.client.post(reverse("user_create"), {
            "username": "uilocalsample",
            "work_location": User.WorkLocation.HEAD_OFFICE,
            "password": "CreateUiPass26!",
            "is_active": "on",
        })
        self.assertRedirects(res_create_ui, reverse("user_list"))
        ui_user = User.objects.get(username="uilocalsample")
        self.assertIsNotNone(ui_user.password_changed_at)
        self.assertFalse(ui_user.must_change_password)
        self.assertTrue(ui_user.check_password("CreateUiPass26!"))

        # 2. Create via Django Admin
        res_create_admin = self.client.post(reverse("admin:accounts_user_add"), {
            "username": "adminlocalsample",
            "usable_password": "true",
            "password1": "CreateAdmPass26!",
            "password2": "CreateAdmPass26!",
        })
        self.assertEqual(res_create_admin.status_code, 302)
        adm_user = User.objects.get(username="adminlocalsample")
        self.assertIsNotNone(adm_user.password_changed_at)
        self.assertFalse(adm_user.must_change_password)
        self.assertTrue(adm_user.check_password("CreateAdmPass26!"))

        # 3. Profile self-change keeps current session, invalidates other sessions
        client_a = Client()
        client_b = Client()
        client_a.force_login(self.target)
        client_b.force_login(self.target)
        self.assertEqual(client_b.get(reverse("dashboard")).status_code, 200)

        res_self = client_a.post(reverse("signature_setup"), {
            "action": "password",
            "password": "SelfNewPass26!",
        })
        self.assertRedirects(res_self, reverse("signature_setup"))
        self.assertEqual(client_a.get(reverse("dashboard")).status_code, 200)
        res_b_after = client_b.get(reverse("dashboard"))
        self.assertEqual(res_b_after.status_code, 302)
        self.assertIn(reverse("login"), res_b_after.url)

        # 4. Admin UI reset sets must_change_password=True, invalidates target sessions, blocks until changed
        self.target.refresh_from_db()
        client_target = Client()
        client_target.force_login(self.target)
        self.assertEqual(client_target.get(reverse("dashboard")).status_code, 200)

        res_ui_reset = self.client.post(reverse("user_edit", args=[self.target.pk]), {
            "username": self.target.username,
            "work_location": User.WorkLocation.HEAD_OFFICE,
            "password": "TempUiReset26!",
            "is_active": "on",
        })
        self.assertRedirects(res_ui_reset, reverse("user_list"))
        self.target.refresh_from_db()
        self.assertTrue(self.target.must_change_password)
        self.assertTrue(self.target.check_password("TempUiReset26!"))

        # Target's pre-existing session was terminated
        res_target_old = client_target.get(reverse("dashboard"))
        self.assertEqual(res_target_old.status_code, 302)
        self.assertIn(reverse("login"), res_target_old.url)

        # Target logs in with temporary password -> must change before accessing web or API
        login_res = client_target.post(reverse("login"), {
            "username": self.target.username,
            "password": "TempUiReset26!",
            "login_mode": "local",
        })
        self.assertRedirects(login_res, reverse("dashboard"), fetch_redirect_response=False)
        self.assertRedirects(
            client_target.get(reverse("dashboard")),
            reverse("forced_password_change"),
        )
        api_blocked = client_target.get(reverse("notification_feed"))
        self.assertEqual(api_blocked.status_code, 403)
        self.assertEqual(api_blocked.json()["error_code"], "PASSWORD_CHANGE_REQUIRED")

        # Complete forced password change on another client_c while client_target holds second session
        client_c = Client()
        client_c.force_login(self.target)
        res_forced = client_c.post(reverse("forced_password_change"), {
            "old_password": "TempUiReset26!",
            "new_password": "FinalUserPass26!",
            "confirm_password": "FinalUserPass26!",
        })
        self.assertRedirects(res_forced, reverse("dashboard"))
        self.target.refresh_from_db()
        self.assertFalse(self.target.must_change_password)
        self.assertEqual(client_c.get(reverse("dashboard")).status_code, 200)
        # Other session invalidated after forced change
        self.assertIn(reverse("login"), client_target.get(reverse("dashboard")).url)

        # 5. Django Admin password change sets must_change_password=True and invalidates target sessions
        client_admin_target = Client()
        client_admin_target.force_login(self.target)
        self.assertEqual(client_admin_target.get(reverse("dashboard")).status_code, 200)

        res_admin_change = self.client.post(
            reverse("admin:auth_user_password_change", args=[self.target.pk]),
            {
                "usable_password": "true",
                "password1": "AdmTmpReset26!",
                "password2": "AdmTmpReset26!",
            },
        )
        self.assertEqual(res_admin_change.status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.must_change_password)
        self.assertTrue(self.target.check_password("AdmTmpReset26!"))
        self.assertIn(reverse("login"), client_admin_target.get(reverse("dashboard")).url)

        # Target logs in with Django Admin temporary password -> forced to change before accessing web or API
        client_admin_target.post(reverse("login"), {
            "username": self.target.username,
            "password": "AdmTmpReset26!",
            "login_mode": "local",
        })
        self.assertRedirects(client_admin_target.get(reverse("dashboard")), reverse("forced_password_change"))
        self.assertEqual(client_admin_target.get(reverse("notification_feed")).status_code, 403)

        # Verify password_changed_at is read-only in Django Admin change page
        admin_change_page = self.client.get(reverse("admin:accounts_user_change", args=[self.target.pk]))
        self.assertEqual(admin_change_page.status_code, 200)
        self.assertNotContains(admin_change_page, 'name="password_changed_at"')

        # Verify no plaintext passwords were ever stored in any audit/transaction log
        self._assert_no_password_in_logs(
            "CreateUiPass26!",
            "CreateAdmPass26!",
            "SelfNewPass26!",
            "TempUiReset26!",
            "FinalUserPass26!",
            "AdmTmpReset26!",
        )

    def test_api_enforcement_before_and_after_expiry_and_exceptions(self):
        self.client.force_login(self.admin)

        # Before expiry: session APIs succeed
        self.assertEqual(self.client.get(reverse("ciam_settings_api")).status_code, 200)
        self.assertEqual(self.client.get(reverse("notification_feed")).status_code, 200)
        self.assertEqual(self.client.get(reverse("system_web_health")).status_code, 200)
        self.assertEqual(
            self.client.post(reverse("theme_preference"), {"mode": User.ThemePreference.DARK}).status_code,
            200,
        )

        # Expire admin password
        self.admin.password_changed_at = timezone.now() - timedelta(days=95)
        self.admin.save(update_fields=["password_changed_at"])

        blocked_calls = [
            ("get", reverse("ciam_settings_api"), {}),
            ("put", reverse("ciam_settings_api"), {"data": json.dumps({"ciam_base_url": "https://evil.example"}), "content_type": "application/json"}),
            ("post", reverse("ciam_test_connection"), {}),
            ("post", reverse("ciam_sync_now"), {}),
            ("post", reverse("sso_break_glass_toggle"), {}),
            ("get", reverse("notification_feed"), {}),
            ("get", reverse("system_web_health"), {}),
            ("post", reverse("theme_preference"), {"data": {"mode": User.ThemePreference.LIGHT}}),
            ("post", reverse("telegram_test_message"), {"data": {"telegram_user_id": "999"}}),
            ("get", reverse("dashboard"), {"HTTP_ACCEPT": "application/json"}),
            ("get", reverse("user_list"), {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}),
            # Authenticated expired user WITHOUT X-Management-API-Key must NOT reach directory endpoint
            ("get", reverse("ciam_directory_accounts_api"), {}),
        ]
        for method, url, kwargs in blocked_calls:
            with self.subTest(method=method, url=url):
                res = getattr(self.client, method)(url, **kwargs)
                self.assertEqual(res.status_code, 403)
                payload = res.json()
                self.assertEqual(payload["error_code"], "PASSWORD_CHANGE_REQUIRED")
                self.assertEqual(payload["code"], "PASSWORD_CHANGE_REQUIRED")
                self.assertEqual(payload["redirect_url"], reverse("forced_password_change"))

        # Confirm CIAM settings were NOT modified by the blocked PUT
        self.assertNotEqual(SystemSettings.objects.get(pk=1).ciam_base_url, "https://evil.example")

        # Allowed exceptions: forced_password_change and logout
        self.assertEqual(self.client.get(reverse("forced_password_change")).status_code, 200)
        # Cannot bypass via other non-allowed views
        self.assertRedirects(self.client.get(reverse("signature_setup")), reverse("forced_password_change"))
        self.assertRedirects(self.client.get(reverse("user_list")), reverse("forced_password_change"))

        # Logout remains accessible and clears session
        logout_res = self.client.post(reverse("logout"))
        self.assertEqual(logout_res.status_code, 302)
        self.assertEqual(logout_res.url, reverse("login"))

        # Re-login and change password via forced_password_change
        self.client.force_login(self.admin)
        change_res = self.client.post(reverse("forced_password_change"), {
            "old_password": "AdminStrong26!",
            "new_password": "AdminRotated26!",
            "confirm_password": "AdminRotated26!",
        })
        self.assertRedirects(change_res, reverse("dashboard"))
        self.assertEqual(self.client.get(reverse("ciam_settings_api")).status_code, 200)

    def test_ad_account_and_service_integrations_not_broken(self):
        # AD admin with password_changed_at=None is never blocked on web or API
        self.client.force_login(self.ad_admin)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("ciam_settings_api")).status_code, 200)
        self.assertEqual(self.client.get(reverse("notification_feed")).status_code, 200)

        # Updating AD account password/settings in Admin keeps must_change_password=False
        self.client.force_login(self.admin)
        res = self.client.post(
            reverse("admin:auth_user_password_change", args=[self.ad_admin.pk]),
            {"password1": "TempAdIgnore26!", "password2": "TempAdIgnore26!"},
        )
        self.assertEqual(res.status_code, 302)
        self.ad_admin.refresh_from_db()
        self.assertFalse(self.ad_admin.must_change_password)
        self.assertEqual(self.ad_admin.check_password_expired(), (False, 90))

        # Service integration 1: M2M directory API with X-Management-API-Key works even if session is expired
        cfg = SystemSettings.objects.get(pk=1)
        cfg.ciam_m2m_key = "test_management_api_key_valid"
        cfg.save(update_fields=["ciam_m2m_key"])
        self.admin.password_changed_at = timezone.now() - timedelta(days=120)
        self.admin.save(update_fields=["password_changed_at"])
        m2m_res = self.client.get(
            reverse("ciam_directory_accounts_api"),
            HTTP_X_MANAGEMENT_API_KEY="test_management_api_key_valid",
        )
        self.assertEqual(m2m_res.status_code, 200)

        # Service integration 2: Public SSO config endpoint without session
        anon_client = Client()
        self.assertEqual(anon_client.get(reverse("sso_config")).status_code, 200)

        # Service integration 3: Telegram webhook not intercepted by PasswordRotationMiddleware
        wh_res = self.client.post(
            reverse("telegram_webhook"),
            data="{}",
            content_type="application/json",
        )
        self.assertNotEqual(wh_res.status_code, 302)
        if wh_res.status_code == 403 and wh_res.headers.get("Content-Type", "").startswith("application/json"):
            self.assertNotEqual(wh_res.json().get("error_code"), "PASSWORD_CHANGE_REQUIRED")

    def test_audit_local_passwords_command_is_readonly_and_accurate(self):
        legacy = User.objects.create_user(
            username="audit_legacy",
            password="LegacyUser26!",
            password_changed_at=None,
        )
        expired_90 = User.objects.create_user(
            username="audit_expired",
            password="ExpiredUser26!",
            password_changed_at=timezone.now() - timedelta(days=90),
        )
        must_chg = User.objects.create_user(
            username="audit_must",
            password="MustUser26!",
            password_changed_at=timezone.now(),
            must_change_password=True,
        )
        session_client = Client()
        session_client.force_login(legacy)

        out = StringIO()
        call_command("audit_local_passwords", json=True, stdout=out)
        report = json.loads(out.getvalue())

        summary = report["summary"]
        self.assertEqual(summary["local_missing_password_changed_at"], 1)
        self.assertEqual(summary["local_expired_90_days"], 1)
        self.assertEqual(summary["local_must_change_password"], 1)
        self.assertEqual(summary["total_affected_local_accounts"], 3)
        self.assertGreaterEqual(summary["affected_active_sessions"], 1)

        usernames = {item["username"] for item in report["affected_accounts"]}
        self.assertEqual(usernames, {legacy.username, expired_90.username, must_chg.username})

        # Ensure command did not fabricate password_changed_at on legacy user
        legacy.refresh_from_db()
        self.assertIsNone(legacy.password_changed_at)

    def test_password_hash_upgrade_preserves_expiry_and_must_change_flag(self):
        old_hash = PBKDF2PasswordHasher().encode("OldValidPass26!", "legacyhashsalt", iterations=100000)

        # Case 1: 100-day expired account undergoes hash upgrade during login/verification
        expired_ts = timezone.now() - timedelta(days=100)
        expired_user = User.objects.create_user(
            username="rehashexpired",
            password=None,
            is_active=True,
            password_changed_at=expired_ts,
            must_change_password=False,
            signature="signatures/rehashexpired.png",
        )
        User.objects.filter(pk=expired_user.pk).update(password=old_hash)
        expired_user.refresh_from_db()
        self.assertEqual(expired_user.password, old_hash)

        authed = authenticate(username="rehashexpired", password="OldValidPass26!")
        self.assertIsNotNone(authed)
        expired_user.refresh_from_db()
        self.assertNotEqual(expired_user.password, old_hash)
        self.assertEqual(expired_user.password_changed_at, expired_ts)
        self.assertFalse(expired_user.must_change_password)
        self.assertEqual(expired_user.check_password_expired(), (True, 0))

        client_exp = Client()
        client_exp.post(reverse("login"), {
            "username": "rehashexpired",
            "password": "OldValidPass26!",
            "login_mode": "local",
        })
        self.assertRedirects(client_exp.get(reverse("dashboard")), reverse("forced_password_change"))
        self.assertEqual(client_exp.get(reverse("notification_feed")).status_code, 403)

        # Case 2: Forced-reset account (must_change_password=True) undergoes hash upgrade on login
        reset_ts = timezone.now() - timedelta(days=2)
        reset_user = User.objects.create_user(
            username="rehashreset",
            password=None,
            is_active=True,
            password_changed_at=reset_ts,
            must_change_password=True,
            signature="signatures/rehashreset.png",
        )
        User.objects.filter(pk=reset_user.pk).update(password=old_hash)
        reset_user.refresh_from_db()

        client_rst = Client()
        login_res = client_rst.post(reverse("login"), {
            "username": "rehashreset",
            "password": "OldValidPass26!",
            "login_mode": "local",
        })
        self.assertRedirects(login_res, reverse("dashboard"), fetch_redirect_response=False)

        reset_user.refresh_from_db()
        self.assertNotEqual(reset_user.password, old_hash)
        self.assertEqual(reset_user.password_changed_at, reset_ts)
        self.assertTrue(reset_user.must_change_password)
        self.assertEqual(reset_user.check_password_expired(), (True, 0))
        self.assertRedirects(client_rst.get(reverse("dashboard")), reverse("forced_password_change"))
        api_res = client_rst.get(reverse("notification_feed"))
        self.assertEqual(api_res.status_code, 403)
        self.assertEqual(api_res.json()["error_code"], "PASSWORD_CHANGE_REQUIRED")
        self.assertEqual(api_res.json()["reason"], "must_change_password")

    def test_common_and_user_attribute_similar_passwords_rejected_across_channels(self):
        emp = User.objects.create_user(
            username="emp901",
            first_name="Somchai",
            last_name="Jaidee",
            email="somchai.wa@example.com",
            password="CurrentStrong26!",
            is_active=True,
            password_changed_at=timezone.now(),
            signature="signatures/emp901.png",
        )

        # Both passwords satisfy complexity rules (8-16, upper, lower, digit, special, no username),
        # so they must be rejected by CommonPasswordValidator and UserAttributeSimilarityValidator.
        weak_cases = (
            ("common", "Password1!"),
            ("similar_to_profile", "Somchai99!"),
        )

        for case_name, weak_pwd in weak_cases:
            with self.subTest(channel="auth_validator", case=case_name):
                with self.assertRaises(ValidationError):
                    validate_password(weak_pwd, emp)

            with self.subTest(channel="profile", case=case_name):
                self.client.force_login(emp)
                res = self.client.post(reverse("signature_setup"), {
                    "action": "password",
                    "password": weak_pwd,
                })
                self.assertEqual(res.status_code, 200)
                self.assertIn("password", res.context["password_form"].errors)
                emp.refresh_from_db()
                self.assertTrue(emp.check_password("CurrentStrong26!"))

            with self.subTest(channel="forced_change", case=case_name):
                res = self.client.post(reverse("forced_password_change"), {
                    "old_password": "CurrentStrong26!",
                    "new_password": weak_pwd,
                    "confirm_password": weak_pwd,
                })
                self.assertEqual(res.status_code, 200)
                self.assertIn("new_password", res.context["form"].errors)
                emp.refresh_from_db()
                self.assertTrue(emp.check_password("CurrentStrong26!"))

            self.client.force_login(self.admin)
            with self.subTest(channel="ui_create", case=case_name):
                res = self.client.post(reverse("user_create"), {
                    "username": "emp902",
                    "first_name": "Somchai",
                    "last_name": "Jaidee",
                    "email": "somchai.wa@example.com",
                    "work_location": User.WorkLocation.HEAD_OFFICE,
                    "password": weak_pwd,
                    "is_active": "on",
                })
                self.assertEqual(res.status_code, 200)
                self.assertIn("password", res.context["form"].errors)
                self.assertFalse(User.objects.filter(username="emp902").exists())

            with self.subTest(channel="ui_edit_reset", case=case_name):
                res = self.client.post(reverse("user_edit", args=[emp.pk]), {
                    "username": emp.username,
                    "first_name": emp.first_name,
                    "last_name": emp.last_name,
                    "email": emp.email,
                    "work_location": User.WorkLocation.HEAD_OFFICE,
                    "password": weak_pwd,
                    "is_active": "on",
                })
                self.assertEqual(res.status_code, 200)
                self.assertIn("password", res.context["form"].errors)
                emp.refresh_from_db()
                self.assertTrue(emp.check_password("CurrentStrong26!"))

            with self.subTest(channel="django_admin_password_change", case=case_name):
                res = self.client.post(reverse("admin:auth_user_password_change", args=[emp.pk]), {
                    "usable_password": "true",
                    "password1": weak_pwd,
                    "password2": weak_pwd,
                })
                self.assertEqual(res.status_code, 200)
                self.assertIn("password2", res.context["adminForm"].form.errors)
                emp.refresh_from_db()
                self.assertTrue(emp.check_password("CurrentStrong26!"))

        # Also verify Django Admin user creation rejects CommonPasswordValidator ("Password1!")
        self.client.force_login(self.admin)
        res_adm_create = self.client.post(reverse("admin:accounts_user_add"), {
            "username": "emp903",
            "usable_password": "true",
            "password1": "Password1!",
            "password2": "Password1!",
        })
        self.assertEqual(res_adm_create.status_code, 200)
        self.assertIn("password2", res_adm_create.context["adminform"].form.errors)
        self.assertFalse(User.objects.filter(username="emp903").exists())
