import re
from datetime import datetime, timezone as dt_timezone
from typing import Any, Optional

from django.core.exceptions import ValidationError
from django.utils import timezone

LOCAL_PASSWORD_EXPIRY_DAYS = 90
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 16
SPECIAL_CHAR_PATTERN = r"[!@#$%^&*()_+\-=\[\]{}|;:,.<>?/~`]"


def validate_password_complexity(
    password: str,
    username: Optional[str] = None,
    *,
    user: Optional[Any] = None,
    forbid_current_password: bool = False,
) -> Optional[str]:
    """
    Enterprise Password Complexity Standard (v2.12.0 Section 13):
    1. Length: 8 - 16 characters
    2. Uppercase: At least 1 (A-Z)
    3. Lowercase: At least 1 (a-z)
    4. Digits: At least 1 (0-9)
    5. Special Characters: At least 1 from (!@#$%^&*()_+-=[]{}|;:,.<>?/~`)
    6. No Username in Password: Case-insensitive check for username length >= 3
    7. Optionally forbid reusing the user's current password
    """
    if not password:
        return "กรุณาระบุรหัสผ่าน"

    pwd = password.strip()
    if len(pwd) < MIN_PASSWORD_LENGTH or len(pwd) > MAX_PASSWORD_LENGTH:
        return f"รหัสผ่านต้องมีความยาวระหว่าง {MIN_PASSWORD_LENGTH} ถึง {MAX_PASSWORD_LENGTH} ตัวอักษร (ปัจจุบัน {len(pwd)} ตัวอักษร)"

    if not re.search(r"[A-Z]", pwd):
        return "รหัสผ่านต้องมีตัวอักษรภาษาอังกฤษพิมพ์ใหญ่ (A-Z) อย่างน้อย 1 ตัว"

    if not re.search(r"[a-z]", pwd):
        return "รหัสผ่านต้องมีตัวอักษรภาษาอังกฤษพิมพ์เล็ก (a-z) อย่างน้อย 1 ตัว"

    if not re.search(r"[0-9]", pwd):
        return "รหัสผ่านต้องมีตัวเลข (0-9) อย่างน้อย 1 ตัว"

    if not re.search(SPECIAL_CHAR_PATTERN, pwd):
        return "รหัสผ่านต้องมีอักขระพิเศษ (!@#$%^&*...) อย่างน้อย 1 ตัว"

    effective_username = username if username is not None else getattr(user, "username", None)
    if effective_username:
        clean_user = effective_username.strip().lower()
        if len(clean_user) >= 3 and clean_user in pwd.lower():
            return "รหัสผ่านต้องไม่มีชื่อผู้ใช้ (Username) ปะปนอยู่"

    if (
        forbid_current_password
        and user is not None
        and getattr(user, "pk", None)
        and getattr(user, "is_local_account", True)
        and user.has_usable_password()
    ):
        if user.check_password(password) or (pwd != password and user.check_password(pwd)):
            return "รหัสผ่านใหม่ต้องไม่ซ้ำกับรหัสผ่านเดิม"

    return None


def _run_standard_django_validators(password: str, user: Optional[Any] = None) -> None:
    """Run configured Django validators excluding LocalPasswordPolicyValidator to avoid recursion."""
    from django.contrib.auth.password_validation import get_default_password_validators

    errors = []
    for validator in get_default_password_validators():
        if isinstance(validator, LocalPasswordPolicyValidator):
            continue
        try:
            validator.validate(password, user)
        except ValidationError as error:
            errors.extend(error.error_list)
    if errors:
        raise ValidationError(errors)


def validate_local_password(
    password: str,
    user: Optional[Any] = None,
    username: Optional[str] = None,
    forbid_current_password: bool = True,
    run_django_validators: bool = True,
) -> None:
    """Validate local password complexity and standard Django weak-password checks."""
    err = validate_password_complexity(
        password,
        username=username,
        user=user,
        forbid_current_password=forbid_current_password,
    )
    if err:
        raise ValidationError(err, code="password_policy")
    if run_django_validators:
        _run_standard_django_validators(password, user)


class LocalPasswordPolicyValidator:
    """Django AUTH_PASSWORD_VALIDATORS adapter backed by validate_local_password."""

    def validate(self, password: str, user: Optional[Any] = None) -> None:
        if user is not None and not getattr(user, "is_local_account", True):
            return
        validate_local_password(
            password,
            user=user,
            username=getattr(user, "username", None) if user is not None else None,
            forbid_current_password=True,
            run_django_validators=False,
        )

    def get_help_text(self) -> str:
        return (
            f"รหัสผ่านต้องมีความยาว {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} ตัวอักษร "
            "มีตัวพิมพ์ใหญ่ พิมพ์เล็ก ตัวเลข อักขระพิเศษ ห้ามมีชื่อผู้ใช้ปะปน และห้ามซ้ำรหัสผ่านเดิม"
        )


def is_password_expired(
    password_changed_at: Optional[datetime],
    created_at: Optional[datetime] = None,
) -> tuple[bool, int]:
    """
    Returns (is_expired: bool, days_left: int) based on 90-day rotation policy.
    Legacy accounts without a recorded password_changed_at are treated as requiring a password change
    instead of fabricating a change date from account creation time.
    """
    if password_changed_at is None:
        return True, 0

    now = timezone.now()
    base_dt = password_changed_at
    if base_dt.tzinfo is None:
        base_dt = base_dt.replace(tzinfo=dt_timezone.utc)

    days_elapsed = max(0, (now - base_dt).days)
    days_left = max(0, LOCAL_PASSWORD_EXPIRY_DAYS - days_elapsed)
    return (days_elapsed >= LOCAL_PASSWORD_EXPIRY_DAYS, days_left)


def apply_local_password(user: Any, raw_password: str, *, mark_must_change: bool = False) -> None:
    """Set password and synchronize password_changed_at and must_change_password consistently."""
    user.set_password(raw_password)
    user.password_changed_at = timezone.now()
    user.must_change_password = bool(mark_must_change and getattr(user, "is_local_account", True))


def invalidate_user_sessions(user: Any, exclude_session_key: Optional[str] = None) -> int:
    """Delete active database sessions for the given user, optionally keeping the current session."""
    user_pk = getattr(user, "pk", None)
    if user_pk is None:
        return 0

    from django.contrib.sessions.models import Session

    target_user_id = str(user_pk)
    keys_to_delete = []
    for session in Session.objects.filter(expire_date__gt=timezone.now()):
        if exclude_session_key and session.session_key == exclude_session_key:
            continue
        try:
            decoded = session.get_decoded()
        except Exception:
            continue
        if str(decoded.get("_auth_user_id")) == target_user_id:
            keys_to_delete.append(session.session_key)

    if keys_to_delete:
        Session.objects.filter(session_key__in=keys_to_delete).delete()
    return len(keys_to_delete)
