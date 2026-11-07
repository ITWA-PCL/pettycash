"""Editable deployment profile; changes never rewrite the active process environment."""
import json
import os
import re
from datetime import time as clock_time
from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render

from .audit import log_event
from .integrations import public_base_url, telegram_token, telegram_secret, telegram_group
from .system_settings import get_system_settings


SECRET_NAMES = {'DJANGO_SECRET_KEY', 'POSTGRES_PASSWORD'}


def current_values():
    database = settings.DATABASES['default']
    return {'DJANGO_SECRET_KEY': settings.SECRET_KEY, 'DJANGO_DEBUG': 'false',
        'DJANGO_ALLOWED_HOSTS': os.getenv('DJANGO_ALLOWED_HOSTS', 'pettycash.windowasia.com'),
        'DJANGO_CSRF_TRUSTED_ORIGINS': os.getenv('DJANGO_CSRF_TRUSTED_ORIGINS', 'https://pettycash.windowasia.com'),
        'PUBLIC_BASE_URL': public_base_url() or 'https://pettycash.windowasia.com',
        'POSTGRES_DB': str(database.get('NAME', '')), 'POSTGRES_USER': str(database.get('USER', '')),
        'POSTGRES_PASSWORD': str(database.get('PASSWORD', '')), 'POSTGRES_HOST': str(database.get('HOST', 'db')),
        'POSTGRES_PORT': str(database.get('PORT') or '5432'), 'REDIS_URL': os.getenv('REDIS_URL', 'redis://redis:6379/0'),
        'PC_INTEGRATIONS_ENABLED': os.getenv('PC_INTEGRATIONS_ENABLED', 'false'),
        'PC_SCHEDULERS_ENABLED': os.getenv('PC_SCHEDULERS_ENABLED', 'false'),
        'TRAEFIK_NETWORK': os.getenv('TRAEFIK_NETWORK', ''), 'TRAEFIK_ENTRYPOINT': os.getenv('TRAEFIK_ENTRYPOINT', ''),
        'TRAEFIK_CERTRESOLVER': os.getenv('TRAEFIK_CERTRESOLVER', '')}


class ServerConfigurationForm(forms.Form):
    @property
    def field_groups(self):
        return [
            ('ฐานข้อมูล PostgreSQL', [self[name] for name in ('POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_HOST', 'POSTGRES_PORT')]),
            ('ความปลอดภัยและที่อยู่เว็บ', [self[name] for name in ('DJANGO_SECRET_KEY', 'DJANGO_DEBUG', 'DJANGO_ALLOWED_HOSTS', 'DJANGO_CSRF_TRUSTED_ORIGINS', 'PUBLIC_BASE_URL')]),
            ('เครือข่ายและบริการของ VPS', [self[name] for name in ('REDIS_URL', 'PC_INTEGRATIONS_ENABLED', 'PC_SCHEDULERS_ENABLED', 'TRAEFIK_NETWORK', 'TRAEFIK_ENTRYPOINT', 'TRAEFIK_CERTRESOLVER')]),
        ]

    DJANGO_SECRET_KEY = forms.CharField(label='Django Secret Key', required=False, strip=False, widget=forms.PasswordInput(render_value=False))
    DJANGO_DEBUG = forms.ChoiceField(label='Debug บนเซิร์ฟเวอร์จริง', choices=[('false', 'ปิด Debug')])
    DJANGO_ALLOWED_HOSTS = forms.CharField(label='ชื่อเว็บที่อนุญาต (Allowed Hosts)')
    DJANGO_CSRF_TRUSTED_ORIGINS = forms.CharField(label='URL ที่อนุญาตส่งข้อมูล (CSRF Trusted Origins)')
    PUBLIC_BASE_URL = forms.URLField(label='URL หลักของเว็บจริง')
    POSTGRES_DB = forms.CharField(label='ชื่อฐานข้อมูล PostgreSQL', max_length=63)
    POSTGRES_USER = forms.CharField(label='ผู้ใช้ PostgreSQL', max_length=63)
    POSTGRES_PASSWORD = forms.CharField(label='รหัสผ่าน PostgreSQL', required=False, strip=False, widget=forms.PasswordInput(render_value=False))
    POSTGRES_HOST = forms.CharField(label='PostgreSQL Host')
    POSTGRES_PORT = forms.IntegerField(label='PostgreSQL Port', min_value=1, max_value=65535)
    REDIS_URL = forms.CharField(label='Redis URL', widget=forms.PasswordInput(render_value=False), required=False)
    PC_INTEGRATIONS_ENABLED = forms.ChoiceField(label='ใช้ค่า AD/Telegram จากไฟล์เซิร์ฟเวอร์', choices=[('false', 'ปิด'), ('true', 'เปิด')])
    PC_SCHEDULERS_ENABLED = forms.ChoiceField(label='งานยืนยันรับเงินอัตโนมัติ / แจ้งเตือน / ล้าง log บน VPS', choices=[('false', 'ปิด'), ('true', 'เปิด')], required=False)
    TRAEFIK_NETWORK = forms.CharField(label='Traefik Network')
    TRAEFIK_ENTRYPOINT = forms.CharField(label='Traefik HTTPS Entrypoint')
    TRAEFIK_CERTRESOLVER = forms.CharField(label='Traefik Certificate Resolver')

    def __init__(self, *args, configuration=None, **kwargs):
        self.configuration = configuration or get_system_settings()
        self.previous = {**current_values(), **json.loads(self.configuration.server_configuration or '{}')}
        super().__init__(*args, initial=self.previous, **kwargs)
        for name in SECRET_NAMES | {'REDIS_URL'}:
            self.fields[name].help_text = 'เว้นว่างเพื่อเก็บค่าเดิม · ไม่แสดงค่าที่บันทึกไว้'

    def clean(self):
        values = super().clean()
        values['PC_SCHEDULERS_ENABLED'] = values.get('PC_SCHEDULERS_ENABLED') or self.previous.get('PC_SCHEDULERS_ENABLED', 'false')
        for name in SECRET_NAMES | {'REDIS_URL'}:
            if not values.get(name):
                values[name] = self.previous.get(name, '')
        key = values.get('DJANGO_SECRET_KEY', '')
        if len(key) < 50 or len(set(key)) < 5:
            self.add_error('DJANGO_SECRET_KEY', 'ใช้ Secret Key ที่ยาวอย่างน้อย 50 ตัวและสุ่มเพียงพอ')
        if not values.get('POSTGRES_PASSWORD'):
            self.add_error('POSTGRES_PASSWORD', 'ระบุรหัสผ่านฐานข้อมูล')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', values.get('POSTGRES_HOST', '')):
            self.add_error('POSTGRES_HOST', 'ระบุชื่อ host หรือ IP ของ PostgreSQL')
        for name, value in list(values.items()):
            if isinstance(value, str) and any(char in value for char in '\r\n\x00'):
                self.add_error(name, 'ค่าแต่ละช่องต้องอยู่ในบรรทัดเดียว')
        try:
            redis = urlsplit(values.get('REDIS_URL', ''))
            redis_ok = redis.scheme in ('redis', 'rediss') and bool(redis.hostname) and (redis.port is None or 1 <= redis.port <= 65535)
        except ValueError:
            redis_ok = False
        if not redis_ok:
            self.add_error('REDIS_URL', 'ระบุ Redis URL ที่ถูกต้อง')
        url = urlsplit(values.get('PUBLIC_BASE_URL', ''))
        if url.scheme != 'https' or not url.hostname or url.path not in ('', '/') or url.username or url.password or url.query or url.fragment:
            self.add_error('PUBLIC_BASE_URL', 'ใช้ URL หลักแบบ HTTPS')
        if values.get('PUBLIC_BASE_URL', '').rstrip('/') != 'https://pettycash.windowasia.com':
            self.add_error('PUBLIC_BASE_URL', 'ชุด deploy นี้ใช้ https://pettycash.windowasia.com; การเปลี่ยนโดเมนต้องปรับ gateway โดยผู้ดูแล VPS ด้วย')
        hosts = [value.strip() for value in values.get('DJANGO_ALLOWED_HOSTS', '').split(',') if value.strip()]
        if any(not re.fullmatch(r'[A-Za-z0-9.-]+', host) for host in hosts) or url.hostname not in hosts:
            self.add_error('DJANGO_ALLOWED_HOSTS', 'ต้องมีชื่อ host ของ URL หลัก และไม่ใช้ *')
        origins = [value.strip().rstrip('/') for value in values.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',') if value.strip()]
        try:
            origins_ok = all(urlsplit(value).scheme == 'https' and urlsplit(value).hostname and not urlsplit(value).path and not urlsplit(value).username and not urlsplit(value).query and not urlsplit(value).fragment for value in origins)
        except ValueError:
            origins_ok = False
        if values.get('PUBLIC_BASE_URL', '').rstrip('/') not in origins or not origins_ok:
            self.add_error('DJANGO_CSRF_TRUSTED_ORIGINS', 'ต้องมี URL หลักและใช้ HTTPS')
        return values

    def save(self):
        self.configuration.server_configuration = json.dumps(self.cleaned_data, ensure_ascii=False)
        self.configuration.save(update_fields=['server_configuration', 'updated_at'])


def environment_text(profile, configuration):
    values = dict(profile)
    reminder_time = configuration.daily_reminder_time
    if isinstance(reminder_time, str):
        reminder_time = clock_time.fromisoformat(reminder_time)
    values.update(AD_GATEWAY_URL=configuration.ad_gateway_url or settings.AD_GATEWAY_URL,
        AD_APP_ID=configuration.ad_app_id or settings.AD_APP_ID, AD_SECRET_KEY=configuration.ad_secret_key or settings.AD_SECRET_KEY,
        AD_ORIGIN_IP=configuration.ad_origin_ip or settings.AD_ORIGIN_IP or '', TELEGRAM_BOT_TOKEN=telegram_token(),
        TELEGRAM_GROUP_CHAT_ID=telegram_group(), TELEGRAM_WEBHOOK_SECRET=telegram_secret(),
        TELEGRAM_API_BASE_URL=settings.TELEGRAM_API_BASE_URL,
        LOG_RETENTION_DAYS=configuration.log_retention_days,
        DAILY_REMINDER_HOUR=reminder_time.hour,
        DAILY_REMINDER_MINUTE=reminder_time.minute)
    # Keep old encryption/session keys available when the operator changes SECRET_KEY.
    new_key = values['DJANGO_SECRET_KEY']
    values['DJANGO_SECRET_KEY_FALLBACKS'] = json.dumps(list(dict.fromkeys(
        key for key in [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS] if key != new_key)))
    lines = ['# Petty Cash deployment profile. Contains secrets; keep outside Git.',
             '# Apply through the owner-approved VPS deployment process; saving this profile does not restart services.']
    for name, value in values.items():
        lines.append(name + '=' + json.dumps(str(value), ensure_ascii=False).replace('$', '$$'))
    return '\n'.join(lines) + '\n'


@login_required
def server_configuration(request):
    from apps.accounts.views import can_manage_users
    if not can_manage_users(request.user):
        return HttpResponseForbidden('สำหรับผู้ดูแลระบบเท่านั้น')
    configuration = get_system_settings()
    if request.method == 'POST' and request.POST.get('action') == 'download':
        profile = json.loads(configuration.server_configuration or '{}')
        if not profile:
            messages.error(request, 'บันทึกชุดค่าก่อนดาวน์โหลด')
            return redirect('server_configuration')
        response = HttpResponse(environment_text(profile, configuration), content_type='text/plain; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="production.env"'
        log_event(request.user, 'EXPORT_SERVER_CONFIGURATION', 'Server configuration', 'ดาวน์โหลดชุดค่าเซิร์ฟเวอร์')
    else:
        form = ServerConfigurationForm(request.POST if request.method == 'POST' else None, configuration=configuration)
        if request.method == 'POST' and form.is_valid():
            form.save()
            log_event(request.user, 'UPDATE_SERVER_CONFIGURATION', 'Server configuration', 'บันทึกชุดค่ารอใช้งาน')
            messages.success(request, 'บันทึกชุดค่ารอใช้งานแล้ว ดาวน์โหลดให้ผู้ดูแล VPS นำไปใช้ได้')
            return redirect('server_configuration')
        response = render(request, 'core/server_configuration.html', {'form': form, 'saved': bool(configuration.server_configuration)})
    response['Cache-Control'] = 'no-store'
    return response
