import base64
import hashlib
import json
import logging
import secrets
from typing import Optional, Tuple
from urllib.parse import urlencode

import jwt
import requests
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import Group
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts.models import User
from apps.core.audit import record_transaction_log
from apps.core.system_settings import get_system_settings

logger = logging.getLogger(__name__)

# Cache JWKS client to avoid fetching on every request
_jwks_clients = {}


def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").replace("=", "")


def get_jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    if jwks_url not in _jwks_clients:
        _jwks_clients[jwks_url] = jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=3600)
    return _jwks_clients[jwks_url]


def generate_pkce_challenge() -> Tuple[str, str]:
    """RFC 7636 PKCE S256 Challenge."""
    verifier = base64url_encode(secrets.token_bytes(32))
    challenge = base64url_encode(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


def build_sso_authorize_url(request: HttpRequest, redirect_uri: str) -> Tuple[str, str, str]:
    """
    Creates PKCE S256 verifier, challenge, state and returns (auth_url, verifier, state).
    """
    config = get_system_settings()
    base_url = (config.ciam_base_url or "https://ciam.windowasia.com").rstrip("/")
    client_id = config.ciam_client_id or "pettycash-spoke-client"

    verifier, challenge = generate_pkce_challenge()
    state = secrets.token_hex(16)

    # Save verifier and state in session for PKCE validation
    request.session["sso_code_verifier"] = verifier
    request.session["sso_state"] = state

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{base_url}/oauth/authorize?{urlencode(params)}"
    return auth_url, verifier, state


def exchange_code_and_login(
    request: HttpRequest,
    code: str,
    code_verifier: Optional[str],
    redirect_uri: str,
    state: Optional[str] = None,
) -> Tuple[Optional[User], Optional[str]]:
    """
    Step 2: Exchange Authorization Code for Tokens at Central IAM.
    Verifies state and PKCE verifier from session, RS256 JWKS, checks claims,
    binds to immutable ciam_sub, provisions or updates user, and logs in.
    """
    config = get_system_settings()

    # 0. Break-Glass & SSO Enabled Guard (Section B.3)
    if not config.ciam_sso_enabled or config.ciam_break_glass_active:
        msg = "Single Sign-On is currently disabled on this application (Break-Glass Mode Active)."
        record_transaction_log(
            category="ciam_sso",
            action="login_failed",
            status="failed",
            message=msg,
            details={"error": "SSODisabledOrBreakGlassActive"},
            triggered_by="system:ciam",
        )
        return None, msg

    # Validate state against session (Anti-CSRF & session fixation protection)
    saved_state = request.session.pop("sso_state", None)
    if not saved_state or not state or saved_state != state:
        msg = "SSO state ไม่ถูกต้องหรือเซสชันหมดอายุ กรุณาลองใหม่อีกครั้ง"
        record_transaction_log(
            category="ciam_sso",
            action="login_failed",
            status="failed",
            message="SSO State Validation Failed",
            details={"reason": "StateMismatchOrExpired", "has_saved_state": bool(saved_state)},
            triggered_by="system:ciam",
        )
        return None, msg

    # Verify session PKCE verifier strictly bound to server session
    saved_verifier = request.session.pop("sso_code_verifier", None)
    if not saved_verifier:
        msg = "ไม่พบรหัสยืนยัน PKCE ในเซสชัน กรุณาลองใหม่อีกครั้ง"
        record_transaction_log(
            category="ciam_sso",
            action="login_failed",
            status="failed",
            message="PKCE Verifier missing from session",
            details={"reason": "MissingSessionVerifier"},
            triggered_by="system:ciam",
        )
        return None, msg
    verifier = saved_verifier

    base_url = (config.ciam_base_url or "https://ciam.windowasia.com").rstrip("/")
    client_id = config.ciam_client_id or "pettycash-spoke-client"
    client_secret = config.ciam_client_secret or ""

    token_url = f"{base_url}/api/v1/oauth/token"
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }

    # 1. Exchange Code
    try:
        res = requests.post(token_url, data=token_payload, timeout=10)
        if res.status_code != 200:
            err_msg = f"Token exchange failed (HTTP {res.status_code}): {res.text[:200]}"
            logger.error("CIAM Token Exchange Error: %s", err_msg)
            record_transaction_log(
                category="ciam_sso",
                action="login_failed",
                status="failed",
                message=err_msg,
                details={"error": "TokenExchangeFailed", "status_code": res.status_code},
                triggered_by="system:ciam",
            )
            return None, "การแลกเปลี่ยนรหัสยืนยันตัวตนกับ Central IAM ล้มเหลว"

        token_data = res.json()
        id_token = token_data.get("id_token")
        if not id_token:
            return None, "ไม่พบ ID Token ในข้อมูลตอบกลับจาก Central IAM"

        # 2. Asymmetric RS256 Signature Verification via JWKS
        jwks_url = f"{base_url}/.well-known/jwks.json"
        jwks_client = get_jwks_client(jwks_url)
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)

        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=base_url,
        )
    except Exception as exc:
        err_msg = f"การตรวจสอบ JWT Token ล้มเหลว: {exc}"
        logger.error("CIAM JWT Decode Error: %s", exc)
        record_transaction_log(
            category="ciam_sso",
            action="login_failed",
            status="failed",
            message=err_msg,
            details={"error": "JwtVerificationError", "exception": str(exc)},
            triggered_by="system:ciam",
        )
        return None, err_msg

    # 3. Read Claims from Central IAM / Active Directory
    ciam_sub = (claims.get("sub") or "").strip()
    username = (claims.get("preferred_username") or ciam_sub or "").strip()
    full_name = claims.get("name") or claims.get("full_name") or ""
    email = claims.get("email") or ""
    department = claims.get("department") or ""

    if not username and not ciam_sub:
        return None, "ไม่พบ Username หรือ Subject Identifier ในข้อมูล Claims จาก Central IAM"

    # Fallback username to sub if username is empty
    if not username:
        username = ciam_sub

    # Split name if possible
    name_parts = full_name.split(" ", 1)
    first_name = name_parts[0] if len(name_parts) > 0 else username
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    # 4. User Resolution & Auto-Provisioning (Security Issue 4: Bind to ciam_sub strictly)
    user = None
    if ciam_sub:
        user = User.objects.filter(ciam_sub=ciam_sub).first()

    if not user:
        # Match by username (legacy or first-time binding)
        candidate = User.objects.filter(username__iexact=username).first()
        if candidate:
            # If candidate already bound to another ciam_sub, reject to prevent account hijacking
            if candidate.ciam_sub and candidate.ciam_sub != ciam_sub:
                msg = f"ปฏิเสธการเข้าสู่ระบบ: บัญชี '{username}' ผูกกับ Subject Identifier อื่นในระบบแล้ว"
                record_transaction_log(
                    category="ciam_sso",
                    action="login_failed",
                    status="failed",
                    message="Subject Identifier Conflict",
                    details={
                        "username": username,
                        "incoming_sub": ciam_sub,
                        "existing_sub": candidate.ciam_sub,
                    },
                    triggered_by=f"user:{username}",
                )
                return None, msg

            # First-time binding of local/AD account to ciam_sub
            if ciam_sub and not candidate.ciam_sub:
                candidate.ciam_sub = ciam_sub
                candidate.save(update_fields=["ciam_sub"])
            user = candidate

    if user:
        # Check active status (Section D.6 - SSO Callback Inactive Guard)
        if not user.is_active:
            msg = f"ปฏิเสธการเข้าสู่ระบบ: บัญชีพนักงาน '{username}' ถูกระงับสิทธิ์ในระบบนี้"
            record_transaction_log(
                category="ciam_sso",
                action="account_deactivated",
                status="warning",
                message=msg,
                details={"username": username, "ciam_sub": ciam_sub, "reason": "is_active is false"},
                triggered_by=f"user:{username}",
            )
            return None, msg

        # Update claims if user exists
        updated_fields = []
        if email and user.email != email:
            user.email = email
            updated_fields.append("email")
        if first_name and not user.first_name:
            user.first_name = first_name
            updated_fields.append("first_name")
        if last_name and not user.last_name:
            user.last_name = last_name
            updated_fields.append("last_name")
        if department and not user.department:
            user.department = department
            updated_fields.append("department")
        if updated_fields:
            user.save(update_fields=updated_fields)

    else:
        # Auto-provision new user with lowest privilege
        default_role_name = config.ciam_auto_provision_group or "ผู้ขอเบิก"
        user = User.objects.create(
            username=username.lower(),
            ciam_sub=ciam_sub or None,
            first_name=first_name,
            last_name=last_name,
            email=email,
            department=department,
            auth_source=User.AuthSource.AD,
            is_ad_auth=True,
            is_active=True,
            password_changed_at=timezone.now(),
            must_change_password=False,
        )
        user.set_unusable_password()
        user.save()

        # Assign least privilege group
        group = Group.objects.filter(name=default_role_name).first()
        if not group:
            from apps.accounts.roles import REQUESTER
            group = Group.objects.filter(name=REQUESTER).first()
        if group:
            user.groups.add(group)

        record_transaction_log(
            category="ciam_sso",
            action="auto_provision_user",
            status="info",
            message=f"สร้างบัญชีผู้ใช้ใหม่อัตโนมัติจาก Central IAM: '{username}'",
            details={
                "username": username,
                "ciam_sub": ciam_sub,
                "email": email,
                "group_assigned": default_role_name,
                "claims": claims,
            },
            triggered_by="system:ciam",
        )

    # 5. Log in user and record success
    login(request, user)
    request.session["auth_provider"] = "ciam_sso"
    request.session["ciam_portal_url"] = f"{base_url}/portal"

    record_transaction_log(
        category="ciam_sso",
        action="login_success",
        status="success",
        message=f"เข้าสู่ระบบผ่าน Central IAM SSO สำเร็จ: ผู้ใช้ '{username}'",
        details={
            "username": username,
            "ciam_sub": ciam_sub,
            "ip": request.META.get("REMOTE_ADDR"),
            "ciam_issuer": base_url,
            "auth_method": "sso_oidc",
        },
        triggered_by=f"user:{username}",
    )

    return user, None

