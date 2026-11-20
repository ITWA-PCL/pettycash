"""Telegram delivery checks without automatic account linking or test-message spam."""
import hashlib
import re

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .integrations import telegram_token
from .models import TelegramBotContact
from .system_settings import get_system_settings

DISMISSED_KEY = 'telegram_connection_reminder_dismissed'


def bot_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def reminder_context(request):
    if not request.user.is_authenticated or getattr(request, 'session', {}).get(DISMISSED_KEY):
        return {}
    if getattr(request, 'resolver_match', None) and request.resolver_match.url_name == 'forced_password_change':
        return {}
    config = get_system_settings()
    token = config.telegram_bot_token or settings.TELEGRAM_BOT_TOKEN
    if not config.telegram_enabled or not token:
        return {}
    user = request.user
    if not user.telegram_user_id or not user.telegram_notifications_enabled:
        return {}
    connected = TelegramBotContact.objects.filter(
        bot_key=bot_key(token), telegram_user_id=str(user.telegram_user_id).strip(),
        confirmed_at__isnull=False, blocked_at__isnull=True,
    ).exists()
    return {} if connected else {'telegram_reminder': 'checking'}


def mark_delivery(chat_id, ready, token=None):
    identity = str(chat_id or '').strip()
    token = token or telegram_token()
    if not identity.isdecimal() or int(identity) <= 0 or not token:
        return
    key = bot_key(token)
    defaults = {'confirmed_at': timezone.now(), 'blocked_at': None} if ready else {'blocked_at': timezone.now()}
    TelegramBotContact.objects.update_or_create(bot_key=key, telegram_user_id=identity, defaults=defaults)
    cache.delete('telegram_delivery_probe:' + key + ':' + identity)


def definitive_delivery_failure(data):
    if not isinstance(data, dict) or data.get('ok') is not False:
        return False
    description = str(data.get('description', '')).lower()
    return data.get('error_code') in (400, 403) and any(reason in description for reason in (
        'chat not found', 'bot was blocked', "bot can't initiate conversation", 'user is deactivated',
    ))


def probe_delivery(chat_id, token):
    """No chat message is sent; Telegram may briefly show the bot typing."""
    identity = str(chat_id).strip()
    key = 'telegram_delivery_probe:' + bot_key(token) + ':' + identity
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        response = requests.post(
            f"{settings.TELEGRAM_API_BASE_URL.rstrip('/')}/bot{token}/sendChatAction",
            json={'chat_id': identity, 'action': 'typing'}, timeout=5,
        )
        data = response.json()
        if isinstance(data, dict) and data.get('ok') is True and data.get('result') is True:
            mark_delivery(identity, True, token)
            return 'ready'
        if definitive_delivery_failure(data):
            mark_delivery(identity, False, token)
            cache.set(key, 'needs_start', 120)
            return 'needs_start'
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        pass
    cache.set(key, 'unknown', 30)
    return 'unknown'


@login_required
@require_POST
@never_cache
def check_connection(request):
    if request.session.get(DISMISSED_KEY):
        return JsonResponse({'ok': True, 'show_reminder': False})
    config = get_system_settings()
    token = config.telegram_bot_token or settings.TELEGRAM_BOT_TOKEN
    user = request.user
    if not config.telegram_enabled or not token or not user.telegram_user_id or not user.telegram_notifications_enabled:
        return JsonResponse({'ok': True, 'show_reminder': False})
    identity = str(user.telegram_user_id).strip()
    if TelegramBotContact.objects.filter(bot_key=bot_key(token), telegram_user_id=identity,
            confirmed_at__isnull=False, blocked_at__isnull=True).exists():
        status = 'ready'
    else:
        status = probe_delivery(identity, token)
    return JsonResponse({'ok': True, 'status': status, 'show_reminder': status == 'needs_start'})


def get_bot_username(token):
    key = 'telegram_bot_username:' + bot_key(token)
    username = cache.get(key)
    if username:
        return username
    try:
        response = requests.get(
            f"{settings.TELEGRAM_API_BASE_URL.rstrip('/')}/bot{token}/getMe", timeout=5,
        )
        response.raise_for_status()
        data = response.json()
        username = data.get('result', {}).get('username') if data.get('ok') is True else None
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
            return None
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        # Never include exception URLs: they contain the bot credential.
        return None
    cache.set(key, username, 900)
    return username


@login_required
@require_GET
@never_cache
def open_bot(request):
    config = get_system_settings()
    token = config.telegram_bot_token or settings.TELEGRAM_BOT_TOKEN
    if not config.telegram_enabled or not token:
        messages.error(request, 'ยังไม่ได้เปิดใช้งาน Telegram กรุณาติดต่อผู้ดูแลระบบ')
        return redirect('dashboard')
    username = get_bot_username(token)
    if not username:
        messages.error(request, 'เปิด Telegram Bot ไม่สำเร็จ กรุณาลองอีกครั้งหรือติดต่อผู้ดูแลระบบ')
        return redirect('dashboard')
    return redirect(f'https://t.me/{username}?start=notifications')


@login_required
@require_POST
@never_cache
def dismiss_reminder(request):
    request.session[DISMISSED_KEY] = True
    if 'application/json' in request.headers.get('Accept', ''):
        return JsonResponse({'ok': True})
    destination = request.POST.get('next', '')
    if not url_has_allowed_host_and_scheme(destination, {request.get_host()}, require_https=request.is_secure()):
        destination = reverse('dashboard')
    return redirect(destination)


def _private_identity(message):
    if not isinstance(message, dict):
        return None
    chat, sender = message.get('chat'), message.get('from')
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    identity = sender.get('id')
    if (chat.get('type') != 'private' or type(identity) is not int or identity <= 0
            or chat.get('id') != identity or sender.get('is_bot')):
        return None
    return str(identity)


def handle_contact_update(update):
    """Called only after the existing webhook secret check; never assigns user IDs."""
    update_id = update.get('update_id')
    if type(update_id) is not int or update_id < 0:
        return False
    kind, identity, blocked = None, None, False
    message = update.get('message')
    if isinstance(message, dict):
        command = str(message.get('text', '')).split(maxsplit=1)[0] if message.get('text') else ''
        if re.fullmatch(r'/start(?:@[A-Za-z0-9_]+)?', command):
            identity, kind = _private_identity(message), 'start'
    membership = update.get('my_chat_member')
    if isinstance(membership, dict):
        member = membership.get('new_chat_member', {})
        if isinstance(member, dict) and member.get('status') in ('kicked', 'member'):
            identity, kind = _private_identity(membership), 'membership'
            blocked = member.get('status') == 'kicked'
    callback = update.get('callback_query')
    if not kind and isinstance(callback, dict):
        private_message = callback.get('message')
        if isinstance(private_message, dict):
            identity = _private_identity({'chat': private_message.get('chat'), 'from': callback.get('from')})
            kind = 'callback'
    if not identity:
        return False
    token = telegram_token()
    if not token or not get_system_settings().telegram_enabled:
        return kind in ('start', 'membership')
    with transaction.atomic():
        contact, _ = TelegramBotContact.objects.get_or_create(bot_key=bot_key(token), telegram_user_id=identity)
        contact = TelegramBotContact.objects.select_for_update().get(pk=contact.pk)
        if update_id <= contact.last_update_id:
            return kind in ('start', 'membership')
        contact.last_update_id = update_id
        if blocked:
            contact.blocked_at = timezone.now()
        else:
            contact.confirmed_at = timezone.now()
            contact.blocked_at = None
        contact.save(update_fields=['last_update_id', 'confirmed_at', 'blocked_at'])
        if kind == 'start':
            from apps.accounts.models import User
            from .audit import log_event
            from .telegram import build_telegram_text, send_telegram_message
            for user in User.objects.filter(is_active=True, telegram_user_id=identity):
                log_event(user, 'TELEGRAM_BOT_STARTED', user.username, 'ยืนยันเปิดใช้งาน Telegram Bot แล้ว')
            text = build_telegram_text('เปิดใช้งาน Telegram Bot แล้ว',
                'รับคำสั่ง Start แล้ว กรุณากลับไปที่เว็บและรีเฟรช หากยังขึ้นเตือน ให้ผู้ดูแลตรวจ Telegram User ID และเปิดรับการแจ้งเตือนให้บัญชีของคุณ')
            transaction.on_commit(lambda: send_telegram_message(identity, text))
    return kind in ('start', 'membership')
