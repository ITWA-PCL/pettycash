"""Production-only settings; leaves developer settings untouched."""
import os
import re
from urllib.parse import urlsplit
from django.core.exceptions import ImproperlyConfigured
from config.settings import *  # noqa: F403


def require(name):
    value = os.environ.get(name, "")
    if not value.strip() or value.startswith("REPLACE_"):
        raise ImproperlyConfigured(f"Missing production setting: {name}")
    return value


SECRET_KEY = require("DJANGO_SECRET_KEY")
if len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be strong and at least 50 characters")
if require("DJANGO_DEBUG").lower() != "false":
    raise ImproperlyConfigured("Production requires DJANGO_DEBUG=false")
DEBUG = False
ALLOWED_HOSTS = [host.strip() for host in require('DJANGO_ALLOWED_HOSTS').split(',') if host.strip()]
if 'pettycash.windowasia.com' not in ALLOWED_HOSTS or any(not re.fullmatch(r'[A-Za-z0-9.-]+', host) for host in ALLOWED_HOSTS):
    raise ImproperlyConfigured('Invalid production allowed hosts')
if require("PUBLIC_BASE_URL") != "https://pettycash.windowasia.com":
    raise ImproperlyConfigured("Unexpected production URL")
CSRF_TRUSTED_ORIGINS = [origin.strip().rstrip('/') for origin in require('DJANGO_CSRF_TRUSTED_ORIGINS').split(',') if origin.strip()]
if 'https://pettycash.windowasia.com' not in CSRF_TRUSTED_ORIGINS or any(urlsplit(origin).scheme != 'https' or not urlsplit(origin).hostname or urlsplit(origin).path or urlsplit(origin).username or urlsplit(origin).query or urlsplit(origin).fragment for origin in CSRF_TRUSTED_ORIGINS):
    raise ImproperlyConfigured('Invalid production CSRF origins')
for name in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
    require(name)
if not re.fullmatch(r'[A-Za-z0-9_.:-]+', require('POSTGRES_HOST')):
    raise ImproperlyConfigured('Invalid database host')
if not require('POSTGRES_PORT').isdigit() or not 1 <= int(require('POSTGRES_PORT')) <= 65535:
    raise ImproperlyConfigured('Invalid database port')
DATABASES["default"]["OPTIONS"] = {"connect_timeout": 3}
try:
    redis_url = urlsplit(require('REDIS_URL'))
    redis_ok = redis_url.scheme in ('redis', 'rediss') and bool(redis_url.hostname) and (redis_url.port is None or 1 <= redis_url.port <= 65535)
except ValueError:
    redis_ok = False
if not redis_ok:
    raise ImproperlyConfigured('Invalid Redis endpoint')

# Only dedicated nginx reaches Gunicorn and replaces the scheme header.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = False
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
# Enable HSTS after TLS and recovery are verified separately.
SECURE_HSTS_SECONDS = 0
STATIC_URL = "/static/"
MEDIA_URL = "/media/"
# This flag clears settings-level values only. Database-backed integration
# overrides must be reviewed separately; this is not a global outbound kill switch.
mode = require("PC_INTEGRATIONS_ENABLED").lower()
if os.getenv('PC_SCHEDULERS_ENABLED', 'false').lower() not in ('true', 'false'):
    raise ImproperlyConfigured('PC_SCHEDULERS_ENABLED must be true or false')
if mode not in {"true", "false"}:
    raise ImproperlyConfigured("PC_INTEGRATIONS_ENABLED must be true or false")
if mode == "false":
    AD_GATEWAY_URL = AD_APP_ID = AD_SECRET_KEY = ""
    TELEGRAM_BOT_TOKEN = TELEGRAM_GROUP_CHAT_ID = TELEGRAM_WEBHOOK_SECRET = ""
