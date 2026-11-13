import json
from typing import Any, Optional
from .models import AdminAuditLog, TransactionLog


def log_event(actor, action: str, target: str = "", detail: str = ""):
    """Store an administrator-visible record of a key business event."""
    return AdminAuditLog.objects.create(actor=actor, action=action, target=target, detail=detail)


def record_transaction_log(
    category: str,
    action: str,
    status: str = "success",
    message: str = "",
    details: Optional[Any] = None,
    records_count: int = 0,
    duration_ms: int = 0,
    triggered_by: str = "system",
) -> TransactionLog:
    """
    Standardized ISO 27001 Security Audit Log (v2.12.0 Section 4).
    """
    if details is not None and not isinstance(details, str):
        try:
            details_str = json.dumps(details, ensure_ascii=False)
        except Exception:
            details_str = str(details)
    else:
        details_str = details or ""

    return TransactionLog.objects.create(
        category=category,
        action=action,
        status=status,
        message=message[:500],
        details=details_str,
        records_count=records_count,
        duration_ms=duration_ms,
        triggered_by=triggered_by[:100],
    )
