"""Read current application settings per operation; no process-level secret cache."""
from django.conf import settings
from .system_settings import get_system_settings


def public_base_url():
    return (get_system_settings().public_base_url or settings.PUBLIC_BASE_URL).rstrip('/')


def telegram_token():
    return get_system_settings().telegram_bot_token or settings.TELEGRAM_BOT_TOKEN


def telegram_secret():
    return get_system_settings().telegram_webhook_secret or settings.TELEGRAM_WEBHOOK_SECRET


def telegram_group():
    configuration = get_system_settings()
    # A bot configured on the web also owns its group setting; blank disables groups.
    if configuration.telegram_bot_token:
        return configuration.telegram_group_chat_id
    return configuration.telegram_group_chat_id or settings.TELEGRAM_GROUP_CHAT_ID
