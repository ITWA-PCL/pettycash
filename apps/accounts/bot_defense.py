import ipaddress
import threading
import time
from typing import Dict, List, Set, Tuple
from django.conf import settings
from django.http import HttpRequest


class SpokeRateLimiter:
    """
    Layer 1: Sliding-Window IP Rate Limiter (Section 12 / หมวด F)
    - 10 requests / 60 seconds per client IP
    - Corporate Whitelist: internal LAN, CIAM server, office WANs
    """
    def __init__(self, limit: int = 10, window: int = 60):
        self.limit = limit
        self.window = window
        self.records: Dict[str, List[float]] = {}
        self.lock = threading.Lock()
        self.whitelist = {
            "127.0.0.1",
            "localhost",
            "::1",
            "172.18.0.1",
            "157.173.219.153",
            "49.231.185.245",
            "58.8.190.63",
        }

    def is_allowed(self, ip: str) -> Tuple[bool, int]:
        if ip in self.whitelist:
            return True, 0

        now = time.time()
        with self.lock:
            timestamps = [t for t in self.records.get(ip, []) if t > (now - self.window)]
            if len(timestamps) >= self.limit:
                retry_after = max(1, int(timestamps[0] + self.window - now))
                self.records[ip] = timestamps
                return False, retry_after

            timestamps.append(now)
            self.records[ip] = timestamps
            return True, 0


spoke_rate_limiter = SpokeRateLimiter(limit=10, window=60)


# Explicit trusted reverse proxy IP set (e.g. localhost/loopback or configured in settings.TRUSTED_PROXIES)
DEFAULT_TRUSTED_PROXIES: Set[str] = {
    "127.0.0.1",
    "::1",
}


def is_trusted_proxy_ip(ip_str: str) -> bool:
    """
    Strict validation that the direct peer REMOTE_ADDR is an authorized reverse proxy.
    Matches settings.TRUSTED_PROXIES if configured, or loopback.
    """
    if not ip_str:
        return False

    configured = getattr(settings, "TRUSTED_PROXIES", None)
    if configured:
        if isinstance(configured, (list, tuple, set)):
            if ip_str in configured:
                return True
            for item in configured:
                try:
                    if "/" in str(item) and ipaddress.ip_address(ip_str) in ipaddress.ip_network(item, strict=False):
                        return True
                except ValueError:
                    continue
        return False

    # Default strictly to loopback addresses
    if ip_str in DEFAULT_TRUSTED_PROXIES:
        return True
    try:
        ip_obj = ipaddress.ip_address(ip_str)
        return ip_obj.is_loopback
    except ValueError:
        return False


def extract_client_ip(request: HttpRequest) -> str:
    """
    Resolve client IP safely without trusting spoofable client headers.
    Only inspects X-Forwarded-For if REMOTE_ADDR is an authorized local reverse proxy.
    """
    remote_addr = (request.META.get("REMOTE_ADDR") or "").strip()

    if is_trusted_proxy_ip(remote_addr):
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        if xff:
            parts = [ip.strip() for ip in xff.split(",") if ip.strip()]
            if parts:
                return parts[0]

    return remote_addr or "127.0.0.1"


def verify_bot_defense(request: HttpRequest) -> Tuple[bool, str, int]:
    """
    Evaluates Layer 1, Layer 2, and Layer 3 Bot Defense.
    Returns (is_passed: bool, error_message: str, http_status_code: int).
    """
    client_ip = extract_client_ip(request)

    # Layer 1: IP Rate Limiting
    allowed, retry_after = spoke_rate_limiter.is_allowed(client_ip)
    if not allowed:
        return False, f"คำขอเข้าสู่ระบบถี่เกินกำหนด กรุณารอ {retry_after} วินาที แล้วลองใหม่อีกครั้ง", 429

    # Layer 2: Decoy Honeypot Trap
    corp_fax = request.POST.get("corporate_fax", "").strip()
    sec_honey = request.POST.get("security_honey", "").strip()
    if corp_fax or sec_honey:
        return False, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", 401

    # Layer 3: Submission Velocity & Timing Guard
    form_render_ts = request.POST.get("form_render_ts", "").strip()
    if form_render_ts and form_render_ts.isdigit():
        now_ms = int(time.time() * 1000)
        render_ms = int(form_render_ts)
        delta_ms = now_ms - render_ms
        if delta_ms < 400 or delta_ms < -10000:
            return False, "ตรวจพบความเร็วการส่งข้อมูลผิดปกติ กรุณาลองใหม่อีกครั้ง", 400

    return True, "", 200

