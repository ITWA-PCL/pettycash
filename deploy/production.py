"""Production-only settings; leaves developer settings untouched."""
import os
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
if require("DJANGO_ALLOWED_HOSTS") != "pettycash.windowasia.com":
    raise ImproperlyConfigured("Unexpected production host")
ALLOWED_HOSTS = ["pettycash.windowasia.com"]
if require("PUBLIC_BASE_URL") != "https://pettycash.windowasia.com":
    raise ImproperlyConfigured("Unexpected production URL")
if require("DJANGO_CSRF_TRUSTED_ORIGINS") != "https://pettycash.windowasia.com":
    raise ImproperlyConfigured("Unexpected CSRF origin")
CSRF_TRUSTED_ORIGINS = ["https://pettycash.windowasia.com"]
for name in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
    require(name)
if require("POSTGRES_HOST") != "db" or require("POSTGRES_PORT") != "5432":
    raise ImproperlyConfigured("Unexpected database endpoint")
DATABASES["default"]["OPTIONS"] = {"connect_timeout": 3}
if require("REDIS_URL") != "redis://redis:6379/0":
    raise ImproperlyConfigured("Unexpected Redis endpoint")

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
if mode not in {"true", "false"}:
    raise ImproperlyConfigured("PC_INTEGRATIONS_ENABLED must be true or false")
if mode == "false":
    AD_GATEWAY_URL = AD_APP_ID = AD_SECRET_KEY = ""
    TELEGRAM_BOT_TOKEN = TELEGRAM_GROUP_CHAT_ID = TELEGRAM_WEBHOOK_SECRET = ""
