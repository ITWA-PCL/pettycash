"""Active Directory Gateway authentication without storing user passwords."""
from datetime import datetime, timezone
import logging

import requests
from django.conf import settings
from apps.core.system_settings import get_system_settings


logger = logging.getLogger(__name__)


class ADGatewayError(Exception):
    pass


def gateway_error_code(response) -> str:
    """Return only known-safe gateway error codes; never expose response payloads."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("message", "")).strip().lower()


def ad_gateway_is_configured():
    configuration = get_system_settings()
    return configuration.ad_enabled and all((configuration.ad_gateway_url or settings.AD_GATEWAY_URL, configuration.ad_app_id or settings.AD_APP_ID, configuration.ad_secret_key or settings.AD_SECRET_KEY))


def verify_ad_credentials(username: str, password: str) -> bool:
    configuration = get_system_settings()
    if not configuration.ad_enabled:
        raise ADGatewayError("ผู้ดูแลระบบปิดการใช้งาน Active Directory ชั่วคราว")
    if not ad_gateway_is_configured():
        raise ADGatewayError("ยังไม่ได้ตั้งค่าการเชื่อมต่อ Active Directory")
    # The live gateway interprets the trailing Z as UTC, despite older integration
    # notes describing it as Bangkok time. Send a real UTC ISO-8601 timestamp.
    gateway_url = configuration.ad_gateway_url or settings.AD_GATEWAY_URL
    app_id = configuration.ad_app_id or settings.AD_APP_ID
    secret_key = configuration.ad_secret_key or settings.AD_SECRET_KEY
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "app_id": app_id,
        "secret_key": secret_key,
        "username": username,
        "password": password,
        "timestamp": timestamp,
    }
    try:
        headers = {"X-Forwarded-For": str(configuration.ad_origin_ip)} if configuration.ad_origin_ip else {}
        response = requests.post(gateway_url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.Timeout as exc:
        raise ADGatewayError("เชื่อมต่อ Active Directory นานเกินกำหนด กรุณาตรวจสอบ AD Gateway") from exc
    except requests.ConnectionError as exc:
        raise ADGatewayError("ไม่สามารถเชื่อมต่อ AD Gateway ได้ กรุณาตรวจสอบว่า service และพอร์ตเปิดใช้งานอยู่") from exc
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "ไม่ทราบสถานะ"
        gateway_code = gateway_error_code(exc.response) if exc.response is not None else ""
        logger.warning(
            "AD Gateway rejected authentication: username=%s timestamp=%s status=%s gateway_code=%s",
            username,
            timestamp,
            status_code,
            gateway_code or "none",
        )
        if "secret mismatch" in gateway_code:
            message = "AD Gateway ปฏิเสธ secret_key เพราะค่าในระบบไม่ตรงกับ Gateway กรุณาอัปเดต AD_SECRET_KEY ในไฟล์ .env แล้วรีสตาร์ต Web"
        elif gateway_code == "expired":
            message = "AD Gateway ปฏิเสธ Timestamp เพราะเวลาไม่ตรงหรือเกิน 5 นาที"
        elif "ip" in gateway_code and ("allow" in gateway_code or "forbid" in gateway_code):
            message = "AD Gateway ไม่อนุญาต Source IP ของเว็บ กรุณาเพิ่ม IP ที่ Gateway เห็นจาก Docker ใน allowlist"
        elif any(word in gateway_code for word in ("invalid credential", "invalid password", "invalid username", "wrong password")):
            message = "ชื่อผู้ใช้หรือรหัสผ่าน Active Directory ไม่ถูกต้อง"
        elif status_code in (401, 403):
            message = f"AD Gateway ปฏิเสธการยืนยันตัวตน (HTTP {status_code}) กรุณาตรวจสอบ app_id, secret_key หรือ IP allowlist"
        elif status_code in (400, 422):
            message = f"AD Gateway ไม่รับข้อมูลที่ส่งไป (HTTP {status_code}) กรุณาตรวจสอบ timestamp และการตั้งค่า Gateway"
        else:
            message = f"AD Gateway ตอบกลับผิดพลาด (HTTP {status_code}) กรุณาตรวจสอบ service และ log ของ Gateway"
        raise ADGatewayError(message) from exc
    except requests.RequestException as exc:
        raise ADGatewayError("AD Gateway ตอบกลับผิดพลาด กรุณาตรวจสอบการตั้งค่า Gateway") from exc
    except ValueError as exc:
        raise ADGatewayError("AD Gateway ส่งผลลัพธ์ไม่ถูกต้อง") from exc

    return (
        data.get("success") is True
        or data.get("authenticated") is True
        or str(data.get("status", "")).lower() in {"success", "ok", "authenticated"}
    )
