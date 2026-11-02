import logging
from html import escape

import requests
from django.conf import settings
from django.utils import timezone
from .system_settings import get_system_settings


logger = logging.getLogger(__name__)


def telegram_is_configured() -> bool:
    return get_system_settings().telegram_enabled and bool(settings.TELEGRAM_BOT_TOKEN)


def build_telegram_text(title: str, message: str, link: str = "", icon: str = "🔔") -> str:
    sent_at = timezone.localtime()
    date_time = f"วันที่ {sent_at:%d/%m}/{sent_at.year + 543} เวลา {sent_at:%H:%M} น."
    text = f"<b>Petty Cash System</b>\n{date_time}\n\n{icon} <b>{escape(title)}</b>\n{escape(message)}".strip()
    if link and settings.PUBLIC_BASE_URL.startswith("https://"):
        detail_url = link if link.startswith(("https://", "http://")) else f"{settings.PUBLIC_BASE_URL.rstrip('/')}/{link.lstrip('/')}"
        text += f'\n\n📄 <a href="{escape(detail_url, quote=True)}">เปิดรายละเอียดคำขอ</a>'
    return text


def telegram_api_request(method: str, payload: dict) -> bool:
    if not telegram_is_configured():
        return False
    try:
        response = requests.post(
            f"{settings.TELEGRAM_API_BASE_URL.rstrip('/')}/bot{settings.TELEGRAM_BOT_TOKEN}/{method}",
            json=payload,
            timeout=8,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Unable to call Telegram method %s", method)
        return False
    return True


def send_telegram_message(chat_id: str, text: str, reply_markup: dict | None = None) -> bool:
    """Send a Telegram message without letting a remote failure stop the web workflow."""
    if not telegram_is_configured() or not chat_id:
        return False

    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return telegram_api_request("sendMessage", payload)


def send_telegram_notification(user, title: str, message: str, link: str = "", reply_markup: dict | None = None, icon: str = "🔔") -> bool:
    """Send a direct Telegram notification when the user has enabled it."""
    if not (user.telegram_notifications_enabled and user.telegram_user_id):
        return False
    return send_telegram_message(user.telegram_user_id, build_telegram_text(title, message, link, icon), reply_markup)


def send_telegram_group_notification(title: str, message: str, link: str = "", icon: str = "🔔") -> bool:
    """Send every workflow event to the optional overview group."""
    return send_telegram_message(settings.TELEGRAM_GROUP_CHAT_ID, build_telegram_text(title, message, link, icon))


def answer_telegram_callback(callback_id: str, text: str) -> bool:
    return telegram_api_request("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def replace_telegram_callback_keyboard(callback: dict, reply_markup: dict) -> bool:
    """Change a callback message's buttons while keeping its request details."""
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if not chat_id or not message_id:
        return False
    return telegram_api_request("editMessageReplyMarkup", {
        "chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup,
    })


def remove_telegram_callback_keyboard(callback: dict) -> bool:
    """Remove buttons from the Telegram message that originated a callback.

    A callback can be delivered after the same request was handled on the web,
    so removing its keyboard also makes stale approval messages unambiguous.
    """
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if not chat_id or not message_id:
        return False
    return telegram_api_request(
        "editMessageReplyMarkup",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        },
    )
