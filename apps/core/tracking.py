"""Compact, requester-only progress data for the dashboard."""

from apps.claims.models import ClaimRequest


ACTIVE_STATUSES = (
    ClaimRequest.Status.PENDING_BUDGET_APPROVAL,
    ClaimRequest.Status.PENDING_SUPERVISOR,
    ClaimRequest.Status.PENDING_EXECUTIVE,
    ClaimRequest.Status.PENDING_ACCOUNTING,
    ClaimRequest.Status.PENDING_PAYMENT,
    ClaimRequest.Status.PAID,
    ClaimRequest.Status.PENDING_CLEARANCE,
    ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW,
    ClaimRequest.Status.PENDING_EXCESS_APPROVAL,
    ClaimRequest.Status.RETURNED,
)


def _person_name(user):
    return (user.get_full_name() or user.username) if user else "—"


def _tracking_card(claim):
    advance = claim.request_type == ClaimRequest.RequestType.ADVANCE
    labels = ["ยื่นคำขอ", "อนุมัติ", "จ่ายเงิน"]
    labels.extend(["รับเงิน / ลงยอดจริง", "บัญชีตรวจ / ปิด"] if advance else ["รับเงิน", "เสร็จสิ้น"])
    status = claim.status
    supplemental_receipt = status == ClaimRequest.Status.PAID and claim.latest_payment_is_supplemental
    if supplemental_receipt:
        labels[-1] = 'รับเงินส่วนต่าง / ปิด'
    waiting_user = None
    headline = claim.workflow_status_display
    if status in (ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED):
        current = 0
        waiting_user = claim.requester
    elif status == ClaimRequest.Status.PENDING_BUDGET_APPROVAL:
        current = 1
        waiting_user = None
        headline = "รอผู้อนุมัติ Budget ทุกแผนก"
    elif status in (ClaimRequest.Status.PENDING_SUPERVISOR, ClaimRequest.Status.PENDING_EXECUTIVE):
        current = 1
        waiting_user = claim.current_approver()
    elif status == ClaimRequest.Status.PENDING_ACCOUNTING:
        current = 1
        waiting_user = claim.current_approver()
    elif status == ClaimRequest.Status.PENDING_PAYMENT:
        current = 2
        waiting_user = claim.accountant
    elif status == ClaimRequest.Status.PAID:
        current = 4 if supplemental_receipt else 3
        waiting_user = claim.requester
        headline = 'รอยืนยันรับเงินส่วนต่าง' if supplemental_receipt else "รอยืนยันรับเงิน"
    elif status == ClaimRequest.Status.PENDING_CLEARANCE:
        current = 3
        waiting_user = claim.requester
        headline = "รอลงยอดใช้จริง"
    elif status == ClaimRequest.Status.PENDING_EXCESS_APPROVAL:
        current = 4
        waiting_user = claim.current_approver()
    elif status == ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW:
        current = 4
        waiting_user = claim.accountant
    else:
        current = len(labels) - 1

    terminal = status in (
        ClaimRequest.Status.COMPLETED,
        ClaimRequest.Status.REJECTED,
        ClaimRequest.Status.CANCELLED,
    )
    steps = []
    for index, label in enumerate(labels):
        if status == ClaimRequest.Status.COMPLETED or index < current:
            state = "done"
        elif index == current and not terminal:
            state = "current"
        else:
            state = "upcoming"
        steps.append({"label": label, "state": state})
    return {
        "claim": claim,
        "headline": headline,
        "waiting_for": _person_name(waiting_user) if waiting_user else "",
        "steps": steps,
        "is_terminal": terminal,
        "is_returned": status == ClaimRequest.Status.RETURNED,
    }


def requester_tracking(user):
    """Show the most recently active own request, then up to two more inline."""
    own = ClaimRequest.objects.filter(requester=user).select_related(
        "requester", "supervisor", "executive", "accounting_manager", "accountant", "cfo",
    )
    active = list(own.filter(status__in=ACTIVE_STATUSES).order_by("-updated_at", "-pk")[:3])
    cards = [_tracking_card(claim) for claim in active]
    return {"featured_tracking": cards[0] if cards else None, "more_tracking": cards[1:]}
