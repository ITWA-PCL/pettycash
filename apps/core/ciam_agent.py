"""
ciam_agent.py — Reverse Heartbeat & Command Worker for Pettycash (Mode C)
Organization: Window Asia Public Company Limited
Complies with Central IAM v2.12.0 Section D (Mode C Reverse Heartbeat & Outbound Sync)
"""
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from django.contrib.auth.models import Group
from django.utils import timezone

from apps.accounts.models import User
from apps.core.audit import record_transaction_log
from apps.core.system_settings import get_system_settings

logger = logging.getLogger("ciam_spoke_agent")

PROTECTED_ACCOUNTS = {"admin", "superadmin", "emergency_admin", "local_admin"}


def execute_local_command(action: str, username: str, reason: str = "") -> Tuple[bool, str]:
    """
    Executes commands received from Central IAM:
    - DISABLE_USER: 1-Click Offboarding
    - ENABLE_USER: Reactivate
    - PROVISION_USER: Two-Way Reconciliation auto-create
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

        config = get_system_settings()
        default_role = config.ciam_auto_provision_group or "ผู้ขอเบิก"
        new_user = User.objects.create(
            username=clean_user,
            auth_source=User.AuthSource.AD,
            is_ad_auth=True,
            is_active=True,
            password_changed_at=timezone.now(),
            must_change_password=False,
        )
        new_user.set_unusable_password()
        new_user.save()

        grp = Group.objects.filter(name=default_role).first()
        if not grp:
            from apps.accounts.roles import REQUESTER
            grp = Group.objects.filter(name=REQUESTER).first()
        if grp:
            new_user.groups.add(grp)

        record_transaction_log(
            category="ciam_sso",
            action="auto_provision_user",
            status="info",
            message=f"สร้างบัญชี '{username}' ในเครื่องสำเร็จตามคำสั่ง Two-Way Reconciliation: {reason}",
            details={"username": username, "reason": reason, "group": default_role},
            triggered_by="system:ciam",
        )
        return True, f"User '{username}' created successfully."

    return False, f"Unknown action: {action}"


def fetch_local_accounts() -> List[Dict[str, Any]]:
    """Dumps user inventory for Full Directory Sync."""
    users = User.objects.all().order_by("username")
    result = []
    for u in users:
        result.append({
            "username": u.username,
            "full_name": u.get_full_name() or u.username,
            "email": u.email,
            "department": u.department,
            "role": u.role_names[0] if u.role_names else "Standard User",
            "is_active": u.is_active,
            "use_ad_auth": u.is_ad_auth,
            "auth_source": u.auth_source,
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
        "sync_type": "FULL_SYNC" if is_full_sync else "HEARTBEAT",
        "command_results": previous_results or [],
    }

    if is_full_sync:
        payload["accounts"] = fetch_local_accounts()

    try:
        res = requests.post(endpoint, json=payload, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
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
                    success, msg = execute_local_command(action, target_user, reason)
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

