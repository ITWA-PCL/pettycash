from .models import AdminAuditLog


def log_event(actor, action: str, target: str = "", detail: str = ""):
    """Store an administrator-visible record of a key business event."""
    return AdminAuditLog.objects.create(actor=actor, action=action, target=target, detail=detail)
