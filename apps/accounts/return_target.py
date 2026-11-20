"""Validated, same-site destinations across authentication and required setup."""
from urllib.parse import urlsplit, urlunsplit

from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

GATE_TARGET = 'required_setup_return_to'
SSO_TARGET = 'sso_return_to'


def safe_target(request, value):
    if not isinstance(value, str) or len(value) > 2048 or '\\' in value:
        return ''
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value):
        return ''
    if not url_has_allowed_host_and_scheme(value, {request.get_host()}, require_https=request.is_secure()):
        return ''
    try:
        parts = urlsplit(value)
    except ValueError:
        return ''
    path = parts.path or '/'
    if not path.startswith('/') or path.startswith('//'):
        return ''
    auth_paths = {reverse(name).rstrip('/') for name in (
        'login', 'logout', 'sso_login_redirect', 'sso_start', 'sso_callback',
        'sso_callback_alias', 'sso_callback_api', 'sso_start_no_slash', 'forced_password_change',
    )}
    if path.rstrip('/') in auth_paths:
        return ''
    return urlunsplit(('', '', path, parts.query, parts.fragment))


def remember_sso_target(request, state):
    target = safe_target(request, request.POST.get('next') or request.GET.get('next') or '')
    # Replace any previous attempt so an unrelated login never inherits its URL.
    request.session[SSO_TARGET] = {'state': state, 'path': target}


def consume_sso_target(request, state):
    pending = request.session.pop(SSO_TARGET, None)
    if not isinstance(pending, dict) or not state or pending.get('state') != state:
        return ''
    return safe_target(request, pending.get('path'))


def remember_setup_target(request):
    if request.method == 'GET':
        target = safe_target(request, request.get_full_path())
        if target:
            request.session[GATE_TARGET] = target


def consume_setup_target(request):
    return safe_target(request, request.session.pop(GATE_TARGET, ''))
