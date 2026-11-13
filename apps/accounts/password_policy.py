import re
from datetime import datetime, timezone
from typing import Optional

LOCAL_PASSWORD_EXPIRY_DAYS = 90
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 16


def validate_password_complexity(password: str, username: Optional[str] = None) -> Optional[str]:
    """
    Enterprise Password Complexity Standard (v2.12.0 Section 13):
    1. Length: 8 - 16 characters
    2. Uppercase: At least 1 (A-Z)
    3. Lowercase: At least 1 (a-z)
    4. Digits: At least 1 (0-9)
    5. Special Characters: At least 1 from (!@#$%^&*()_+-=[]{}|;:,.<>?/~`)
    6. No Username in Password: Case-insensitive check for username length >= 3
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

    if not re.search(r"[!@#$%^&*()_+\-=\[\]{}|;:,.<>?/~`]", pwd):
        return "รหัสผ่านต้องมีอักขระพิเศษ (!@#$%^&*...) อย่างน้อย 1 ตัว"

    if username:
        clean_user = username.strip().lower()
        if len(clean_user) >= 3 and clean_user in pwd.lower():
            return "รหัสผ่านต้องไม่มีชื่อผู้ใช้ (Username) ปะปนอยู่"

    return None


def is_password_expired(password_changed_at: Optional[datetime], created_at: Optional[datetime] = None) -> tuple[bool, int]:
    """
    Returns (is_expired: bool, days_left: int) based on 90-day rotation policy.
    """
    now = datetime.now(timezone.utc)
    base_dt = password_changed_at or created_at or now
    if base_dt.tzinfo is None:
        base_dt = base_dt.replace(tzinfo=timezone.utc)

    days_elapsed = (now - base_dt).days
    days_left = max(0, LOCAL_PASSWORD_EXPIRY_DAYS - days_elapsed)
    return (days_elapsed >= LOCAL_PASSWORD_EXPIRY_DAYS, days_left)

