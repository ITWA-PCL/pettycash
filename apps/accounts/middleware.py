from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from apps.core.system_settings import get_system_settings


def _is_m2m_directory_request(request) -> bool:
    has_m2m_key = bool(
        request.headers.get("X-Management-API-Key")
        or request.META.get("HTTP_X_MANAGEMENT_API_KEY")
    )
    return has_m2m_key and request.path.rstrip("/") == "/api/v1/directory/accounts"


def _is_api_request(request) -> bool:
    if request.path.startswith("/api/"):
        return True
    api_named_paths = {
        reverse("notification_feed"),
        reverse("system_web_health"),
        reverse("theme_preference"),
        reverse("telegram_test_message"),
    }
    if request.path in api_named_paths:
        return True
    accept = request.headers.get("Accept", "").lower()
    if "application/json" in accept:
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    return False


class PasswordRotationMiddleware:
    """
    Enterprise ISO 27001 90-Day Forced Password Rotation Policy (Section 13).
    Enforced strictly for local accounts only (non-AD).
    Blocks access to web views and session-authenticated APIs when password has
    expired (>= 90 days), has no recorded password_changed_at, or must_change_password is true.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and getattr(user, "is_local_account", False):
            is_expired, _ = user.check_password_expired()
            if is_expired:
                allowed_paths = {
                    reverse("forced_password_change"),
                    reverse("logout"),
                    reverse("telegram_webhook"),
                }
                if (
                    request.path not in allowed_paths
                    and not request.path.startswith(("/static/", "/media/"))
                    and not _is_m2m_directory_request(request)
                ):
                    if _is_api_request(request):
                        reason = (
                            "must_change_password"
                            if user.must_change_password
                            else ("password_changed_at_missing" if user.password_changed_at is None else "password_expired")
                        )
                        response = JsonResponse(
                            {
                                "ok": False,
                                "status": "error",
                                "code": "PASSWORD_CHANGE_REQUIRED",
                                "error_code": "PASSWORD_CHANGE_REQUIRED",
                                "reason": reason,
                                "detail": "รหัสผ่านหมดอายุหรือต้องเปลี่ยนรหัสผ่านก่อนใช้งาน กรุณาเปลี่ยนรหัสผ่านใหม่",
                                "message": "รหัสผ่านหมดอายุหรือต้องเปลี่ยนรหัสผ่านก่อนใช้งาน กรุณาเปลี่ยนรหัสผ่านใหม่",
                                "redirect_url": reverse("forced_password_change"),
                            },
                            status=403,
                        )
                        response["Cache-Control"] = "no-store"
                        return response
                    return redirect("forced_password_change")

        return self.get_response(request)


class SignatureOnboardingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        allowed_paths = {
            reverse("login"),
            reverse("logout"),
            reverse("signature_setup"),
            reverse("signature_image"),
            reverse("theme_preference"),
            reverse("forced_password_change"),
        }
        if (
            user.is_authenticated
            and not user.has_signature
            and request.path not in allowed_paths
            and not request.path.startswith(("/static/", "/media/", "/api/"))
            and get_system_settings().require_signature_on_first_login
        ):
            return redirect("signature_setup")
        return self.get_response(request)
