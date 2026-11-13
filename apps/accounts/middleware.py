from django.shortcuts import redirect
from django.urls import reverse
from apps.core.system_settings import get_system_settings


class PasswordRotationMiddleware:
    """
    Enterprise ISO 27001 90-Day Forced Password Rotation Policy (Section 13).
    Enforced strictly for local accounts only (non-AD).
    Blocks access to the system when password has expired (> 90 days)
    or must_change_password is true, forcing the user to change their password.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and user.is_local_account:
            is_expired, _ = user.check_password_expired()
            if is_expired:
                allowed_paths = {
                    reverse("forced_password_change"),
                    reverse("logout"),
                }
                if request.path not in allowed_paths and not request.path.startswith(("/static/", "/media/", "/api/")):
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
