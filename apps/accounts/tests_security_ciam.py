import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from django.http import HttpRequest
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.bot_defense import verify_bot_defense, SpokeRateLimiter
from apps.accounts.models import User
from apps.accounts.password_policy import is_password_expired, validate_password_complexity
from apps.accounts.roles import SUPER_ADMIN
from apps.core.ciam_agent import execute_local_command, fetch_local_accounts
from apps.core.models import SystemSettings, TransactionLog


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
