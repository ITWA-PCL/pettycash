"""Encrypted application credentials. The server SECRET_KEY must remain stable."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def cipher():
    keys = [settings.SECRET_KEY, *getattr(settings, 'SECRET_KEY_FALLBACKS', [])]
    return MultiFernet([Fernet(base64.urlsafe_b64encode(hashlib.sha256(
        ('pettycash-settings-v1:' + key).encode()).digest())) for key in keys])


class SecretTextField(models.TextField):
    prefix = 'encrypted:v1:'

    def from_db_value(self, value, expression, connection):
        if not value or not value.startswith(self.prefix):
            return value
        try:
            return cipher().decrypt(value[len(self.prefix):].encode()).decode()
        except InvalidToken:
            raise ImproperlyConfigured('ไม่สามารถอ่านค่าลับได้ กรุณาตรวจ SECRET_KEY ของเซิร์ฟเวอร์') from None

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return self.prefix + cipher().encrypt(value.encode()).decode() if value else value
