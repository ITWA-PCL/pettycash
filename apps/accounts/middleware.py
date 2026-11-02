from django.shortcuts import redirect
from django.urls import reverse
from apps.core.system_settings import get_system_settings


class SignatureOnboardingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        allowed_paths = {
            reverse("login"),
            reverse("logout"),
            reverse("signature_setup"),
            reverse("theme_preference"),
        }
        if (
            user.is_authenticated
            and not user.has_signature
            and request.path not in allowed_paths
            and not request.path.startswith(("/static/", "/media/"))
            and get_system_settings().require_signature_on_first_login
        ):
            return redirect("signature_setup")
        return self.get_response(request)
