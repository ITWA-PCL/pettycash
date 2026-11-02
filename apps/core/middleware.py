from django.shortcuts import render

from apps.accounts.roles import SUPER_ADMIN
from .system_settings import get_system_settings


class MaintenanceModeMiddleware:
    """Show a safe maintenance page to everyone except system administrators."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(("/static/", "/media/", "/logout/")):
            return self.get_response(request)
        try:
            configuration = get_system_settings()
        except Exception:
            return self.get_response(request)
        user = request.user
        is_admin = user.is_authenticated and (user.is_superuser or user.has_role(SUPER_ADMIN))
        is_admin_login = request.path.startswith("/login/") and request.GET.get("maintenance_admin") == "1"
        if configuration.maintenance_mode and not is_admin and not is_admin_login:
            return render(request, "core/maintenance.html", {"message": configuration.maintenance_message}, status=503)
        return self.get_response(request)
