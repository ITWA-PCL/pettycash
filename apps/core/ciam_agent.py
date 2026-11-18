"""
ciam_agent.py — Reverse Heartbeat & Command Worker for Pettycash (Mode C)
Organization: Window Asia Public Company Limited
Complies with Central IAM v2.13.0 Section C & Section D (Mode C Reverse Heartbeat & Outbound Sync)
"""
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.password_policy import invalidate_user_sessions
from apps.accounts.roles import REQUESTER, SUPER_ADMIN
from apps.core.audit import record_transaction_log
from apps.core.system_settings import get_system_settings

logger = logging.getLogger("ciam_spoke_agent")

CIAM_FRAMEWORK_VERSION = "2.13.0"
PROTECTED_ACCOUNTS = {"admin", "superadmin", "emergency_admin", "local_admin"}


def _resolve_least_privilege_group(requested_role: str = "") -> Optional[Group]:
    """Resolve provisioned user group following Least Privilege (never assign SUPER_ADMIN)."""
    config = get_system_settings()
    if requested_role and requested_role != SUPER_ADMIN:
        grp = Group.objects.filter(name=requested_role).first()
        if grp is not None:
            return grp

    default_role = (config.ciam_auto_provision_group or "").strip()
    if default_role and default_role != SUPER_ADMIN:
        grp = Group.objects.filter(name=default_role).first()
        if grp is not None:
            return grp

    return Group.objects.filter(name=REQUESTER).first()


def provision_ciam_user(
    username: str,
    account_meta: Optional[Dict[str, Any]] = None,
    *,
    reason: str = "",
    triggered_by: str = "system:ciam",
) -> Tuple[User, Optional[Group]]:
    """
    Create and commit a real User record from Central IAM provisioning metadata.
    Supports both One-Way AD accounts (use_ad_auth=True) and Local accounts
    with initial password & forced rotation (use_ad_auth=False).
    Never logs plaintext passwords.
    """
    meta = account_meta or {}
    full_name = str(meta.get("full_name") or "").strip()
    email = str(meta.get("email") or "").strip()
    department = str(meta.get("department") or "").strip()
    requested_role = str(meta.get("role") or meta.get("group_name") or "").strip()
    use_ad_auth = bool(meta.get("use_ad_auth", True))
    raw_password = str(meta.get("password") or "").strip()
    must_change_password = bool(meta.get("must_change_password", True)) if not use_ad_auth else False

    name_parts = full_name.split(" ", 1) if full_name else []
    first_name = name_parts[0] if len(name_parts) > 0 else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    with transaction.atomic():
        now = timezone.now()
        new_user = User(
            username=username,
            first_name=first_name,
            last_name=last_name,
            email=email,
            department=department,
            auth_source=User.AuthSource.AD if use_ad_auth else User.AuthSource.LOCAL,
            is_ad_auth=use_ad_auth,
            is_active=True,
            password_changed_at=now,
            must_change_password=must_change_password,
        )
        if use_ad_auth:
            new_user.set_unusable_password()
            new_user.must_change_password = False
        elif raw_password:
            # User.set_password() resets must_change_password=False, so re-apply after calling it.
            new_user.set_password(raw_password)
            new_user.must_change_password = must_change_password
            new_user.password_changed_at = now
        else:
            new_user.set_unusable_password()
            new_user.must_change_password = must_change_password

        new_user.save()

        grp = _resolve_least_privilege_group(requested_role)
        if grp is not None:
            new_user.groups.add(grp)

        record_transaction_log(
            category="ciam_sso",
            action="auto_provision_user",
            status="info",
            message=f"สร้างบัญชี '{new_user.username}' ในระบบสำเร็จจาก Central IAM: {reason}",
            details={
                "username": new_user.username,
                "use_ad_auth": use_ad_auth,
                "must_change_password": new_user.must_change_password,
                "group": grp.name if grp else "",
                "reason": reason,
            },
            triggered_by=triggered_by,
        )

    return new_user, grp


def execute_local_command(
    action: str,
    username: str,
    reason: str = "",
    account_meta: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Executes commands received from Central IAM:
    - DISABLE_USER: 1-Click Offboarding (revokes active sessions immediately)
    - ENABLE_USER: Reactivate
    - PROVISION_USER: Two-Way Reconciliation auto-create (AD or Local account)
    """
    clean_user = username.strip().lower()

    if action == "DISABLE_USER":
        # Guardrail: protect emergency / superuser administrators
        if clean_user in PROTECTED_ACCOUNTS:
            msg = f"Protected account: Cannot disable emergency administrator '{username}'"
            logger.warning("🛡️ %s", msg)
            return False, msg

        try:
            user = User.objects.get(username__iexact=clean_user)
            if user.is_superuser:
                return False, f"Protected account: Cannot disable superuser '{username}'"
            user.is_active = False
            user.save(update_fields=["is_active"])
            invalidate_user_sessions(user)
            record_transaction_log(
                category="ciam_sso",
                action="account_deactivated",
                status="warning",
                message=f"ระงับสิทธิ์บัญชีพนักงาน '{username}' ตามคำสั่ง 1-Click Offboarding: {reason}",
                details={"username": username, "reason": reason},
                triggered_by="system:ciam",
            )
            return True, f"User '{username}' disabled successfully."
        except User.DoesNotExist:
            return False, f"User '{username}' not found in local database."

    elif action == "ENABLE_USER":
        try:
            user = User.objects.get(username__iexact=clean_user)
            user.is_active = True
            user.save(update_fields=["is_active"])
            record_transaction_log(
                category="ciam_sso",
                action="account_reactivated",
                status="info",
                message=f"คืนสิทธิ์บัญชีพนักงาน '{username}' ตามคำสั่ง Central IAM: {reason}",
                details={"username": username, "reason": reason},
                triggered_by="system:ciam",
            )
            return True, f"User '{username}' enabled successfully."
        except User.DoesNotExist:
            return False, f"User '{username}' not found in local database."

    elif action == "PROVISION_USER":
        if User.objects.filter(username__iexact=clean_user).exists():
            return True, f"User '{username}' already exists."

        new_user, grp = provision_ciam_user(
            clean_user,
            account_meta=account_meta,
            reason=reason,
            triggered_by="system:ciam",
        )
        return True, (
            f"User '{new_user.username}' created successfully "
            f"(role={grp.name if grp else 'none'}, use_ad_auth={new_user.is_ad_auth})."
        )

    return False, f"Unknown action: {action}"


def fetch_local_accounts() -> List[Dict[str, Any]]:
    """Dumps user inventory for Full Directory Sync and M2M Directory API (Section C.1)."""
    users = User.objects.prefetch_related("groups").all().order_by("username")
    result = []
    for u in users:
        role_names = u.role_names
        primary_group = role_names[0] if role_names else "Standard User"
        updated_ref = u.password_changed_at or u.last_login or u.date_joined
        result.append({
            "id": u.pk,
            "username": u.username,
            "full_name": u.get_full_name() or u.username,
            "email": u.email,
            "department": u.department,
            "telegram_chat_id": u.telegram_user_id or None,
            "role": primary_group,
            "group_name": primary_group,
            "is_active": u.is_active,
            "use_ad_auth": u.is_ad_auth,
            "auth_source": u.auth_source,
            "last_login_at": u.last_login.isoformat() if u.last_login else None,
            "created_at": u.date_joined.isoformat() if u.date_joined else None,
            "updated_at": updated_ref.isoformat() if updated_ref else None,
        })
    return result


def run_agent_cycle(is_full_sync: bool = False, previous_results: Optional[List] = None) -> Tuple[List[Dict], bool, Dict[str, Any]]:
    """
    Sends one Outbound POST Heartbeat to Central IAM.
    Returns (new_results, should_full_sync_next, response_data).
    """
    config = get_system_settings()
    base_url = (config.ciam_base_url or "https://ciam.windowasia.com").rstrip("/")
    client_id = config.ciam_client_id or "pettycash-spoke-client"
    api_key = config.ciam_m2m_key or config.ciam_client_secret or ""

    endpoint = f"{base_url}/api/v1/agent/heartbeat"
    headers = {
        "Content-Type": "application/json",
        "X-Spoke-Client-ID": client_id,
        "X-Spoke-API-Key": api_key,
        "X-Request-Timestamp": str(int(time.time())),
    }
    payload = {
        "app_code": "pettycash",
        "status": "HEALTHY",
        "framework_version": CIAM_FRAMEWORK_VERSION,
        "sync_type": "FULL_SYNC" if is_full_sync else "HEARTBEAT",
        "command_results": previous_results or [],
    }

    if is_full_sync:
        payload["accounts"] = fetch_local_accounts()

    try:
        res = requests.post(endpoint, json=payload, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            assigned_map = {
                (a.get("username") or "").strip().lower(): a
                for a in (data.get("assigned_accounts") or [])
                if isinstance(a, dict) and a.get("username")
            }
            pending_commands = data.get("pending_commands", [])
            new_results = []
            should_sync_next = False

            for cmd in pending_commands:
                cmd_id = cmd.get("command_id", "")
                action = cmd.get("action", "")
                target_user = cmd.get("username", "")
                reason = cmd.get("reason", "")

                if action == "REQUEST_FULL_SYNC":
                    should_sync_next = True
                    new_results.append({
                        "command_id": cmd_id,
                        "action": action,
                        "username": target_user,
                        "status": "COMPLETED",
                        "message": "Full sync command acknowledged. Directory inventory will be pushed.",
                    })
                else:
                    meta = assigned_map.get(target_user.strip().lower()) or cmd
                    success, msg = execute_local_command(action, target_user, reason, account_meta=meta)
                    new_results.append({
                        "command_id": cmd_id,
                        "action": action,
                        "username": target_user,
                        "status": "COMPLETED" if success else "FAILED",
                        "message": msg,
                    })

            return new_results, should_sync_next, data
        else:
            logger.warning("CIAM returned status %s: %s", res.status_code, res.text[:200])
            return previous_results or [], False, {"error": f"HTTP {res.status_code}: {res.text[:200]}"}
    except Exception as exc:
        logger.error("Failed to connect to Central IAM: %s", exc)
        return previous_results or [], False, {"error": str(exc)}

