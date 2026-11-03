from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import connection, transaction
from django.conf import settings
from django.db.models import Count, Q, Sum
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from urllib.parse import urlparse
import socket
from uuid import uuid4
from datetime import date, timedelta
from decimal import Decimal

from apps.accounts.views import can_manage_users
from apps.claims.models import (
    ClaimApproval, ClaimRequest, REQUESTER_STATUS_GROUPS, pending_cfo_acknowledgement_ids, pending_cfo_approval_q,
)
from apps.accounts.models import User
from apps.accounts.roles import ACCOUNTANT, ACCOUNTING_APPROVER, EXECUTIVE_APPROVER, REQUESTER, SUPER_ADMIN, SUPERVISOR_APPROVER
from .forms import BudgetForm, BusinessUnitForm, SystemSettingsForm
from .system_settings import get_system_settings
from .models import AUDIT_ACTION_LABELS, AdminAuditLog, Budget, BusinessUnit, Notification, TelegramWebhookCheck
from .audit import log_event
from .telegram import build_telegram_text, send_telegram_message, telegram_api_request
from .pagination import paginate
from .system_health import get_web_health
from .tracking import requester_tracking


MONITOR_STATUS_META = (
    (ClaimRequest.Status.DRAFT, "แบบร่าง", "#94a3b8"),
    (ClaimRequest.Status.PENDING_BUDGET_APPROVAL, "รออนุมัติ Budget", "#b486d4"),
    (ClaimRequest.Status.PENDING_SUPERVISOR, "รอผู้อนุมัติ", "#e2aa30"),
    (ClaimRequest.Status.PENDING_EXECUTIVE, "รอผู้บริหาร", "#d98736"),
    (ClaimRequest.Status.PENDING_ACCOUNTING, "รอบัญชีอนุมัติ", "#8f6bd7"),
    (ClaimRequest.Status.PENDING_PAYMENT, "รอจ่ายเงิน", "#378bda"),
    (ClaimRequest.Status.PAID, "รอยืนยันรับเงิน", "#1f9fbb"),
    (ClaimRequest.Status.PENDING_CLEARANCE, "รอเคลียร์", "#476fd1"),
    (ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW, "รอบัญชีตรวจ", "#476fd1"),
    (ClaimRequest.Status.PENDING_EXCESS_APPROVAL, "รออนุมัติส่วนต่าง", "#c56bba"),
    (ClaimRequest.Status.COMPLETED, "เสร็จสิ้น", "#24955d"),
    (ClaimRequest.Status.RETURNED, "ส่งกลับแก้ไข", "#dd8333"),
    (ClaimRequest.Status.REJECTED, "ไม่อนุมัติ", "#c45b5b"),
    (ClaimRequest.Status.CANCELLED, "ยกเลิก", "#7c8796"),
)


def can_manage_budget_master(user):
    return (
        user.is_superuser or user.has_role(SUPER_ADMIN)
        or user.has_role(ACCOUNTANT) or user.has_role(ACCOUNTING_APPROVER)
        or get_system_settings().cfo_id == user.pk
    )


@login_required
def budget_settings(request):
    can_manage = can_manage_budget_master(request.user)
    if request.method == "POST" and not can_manage:
        return HttpResponseForbidden("คุณไม่มีสิทธิ์จัดการ Budget")
    business_units = BusinessUnit.objects.all() if can_manage else BusinessUnit.objects.none()
    budgets = Budget.objects.all() if can_manage else Budget.objects.filter(department=request.user.department)
    budgets = budgets.order_by("department", "name", "pk")
    editing = get_object_or_404(budgets, pk=request.GET["edit"]) if can_manage and request.GET.get("edit") else None
    editing_bu = get_object_or_404(business_units, pk=request.GET["edit_bu"]) if can_manage and request.GET.get("edit_bu") else None
    budget_id = request.POST.get("budget_id")
    budget = get_object_or_404(budgets, pk=budget_id) if budget_id else None
    bu_id = request.POST.get("bu_id")
    business_unit = get_object_or_404(business_units, pk=bu_id) if bu_id else None
    is_bu_post = request.method == "POST" and request.POST.get("form_type") == "business_unit"
    if request.method == "POST" and budget and not is_bu_post:
        editing = budget
    if is_bu_post and business_unit:
        editing_bu = business_unit
    form = BudgetForm(request.POST if request.method == "POST" and not is_bu_post else None, instance=budget or editing)
    bu_form = BusinessUnitForm(request.POST if is_bu_post else None, instance=business_unit or editing_bu)
    if is_bu_post and bu_form.is_valid():
        saved_bu = bu_form.save()
        log_event(request.user, "UPDATE_BU" if business_unit else "CREATE_BU", saved_bu.code, saved_bu.name)
        messages.success(request, "บันทึก BU เรียบร้อยแล้ว")
        return redirect("budget_settings")
    if request.method == "POST" and not is_bu_post and form.is_valid():
        budget = form.save(commit=False)
        budget.owner = request.user
        budget.save()
        action = "UPDATE_BUDGET" if budget_id else "CREATE_BUDGET"
        log_event(request.user, action, budget.name, f"แผนก {budget.department} · {budget.get_category_display()}")
        messages.success(request, "บันทึก Budget เรียบร้อยแล้ว")
        return redirect("budget_settings")
    return render(request, "core/budget_settings.html", {"form": form, "bu_form": bu_form, "business_units": business_units, "budgets": list(budgets), "editing": editing, "editing_bu": editing_bu, "can_manage_budgets": can_manage})


@login_required
def budget_delete(request, budget_id):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed"}, status=405)
    if not can_manage_budget_master(request.user):
        return HttpResponseForbidden("คุณไม่มีสิทธิ์จัดการ Budget")
    budget = get_object_or_404(Budget, pk=budget_id)
    log_event(request.user, "DELETE_BUDGET", budget.name, f"แผนก {budget.department} · {budget.get_category_display()}")
    budget.delete()
    messages.success(request, "ลบ Budget เรียบร้อยแล้ว")
    return redirect("budget_settings")


@login_required
@transaction.atomic
def business_unit_delete(request, bu_id):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed"}, status=405)
    if not can_manage_budget_master(request.user):
        return HttpResponseForbidden("คุณไม่มีสิทธิ์จัดการ BU")
    unit = get_object_or_404(BusinessUnit.objects.select_for_update(), pk=bu_id)
    # Claims and users retain their BU code snapshots; only remove the master option.
    log_event(request.user, "DELETE_BU", unit.code, unit.name)
    unit.delete()
    messages.success(request, "ลบ BU จากรายการให้เลือกแล้ว ข้อมูลคำขอเดิมไม่เปลี่ยนแปลง")
    return redirect("budget_settings")


def _monitor_date(value, fallback):
    try:
        return date.fromisoformat(value) if value else fallback
    except ValueError:
        return fallback


def _audit_search_key(value):
    """Normalize common Thai audit terms so visible labels are searchable."""
    return str(value).casefold().replace(" ", "").replace("คำขอ", "").replace("รายการ", "")


@login_required
def dashboard(request):
    is_system_admin = can_manage_users(request.user)
    configuration = get_system_settings()
    own_claims = ClaimRequest.objects.filter(requester=request.user)
    claims = ClaimRequest.objects.all() if is_system_admin else ClaimRequest.objects.filter(
        Q(requester=request.user)
        | Q(budget_approvals__approver=request.user, budget_approvals__approved_at__isnull=True, status=ClaimRequest.Status.PENDING_BUDGET_APPROVAL)
        | Q(supervisor=request.user, status=ClaimRequest.Status.PENDING_SUPERVISOR)
        | Q(executive=request.user, status=ClaimRequest.Status.PENDING_EXECUTIVE)
        | Q(accounting_manager=request.user, status__in=(ClaimRequest.Status.PENDING_ACCOUNTING, ClaimRequest.Status.PENDING_EXCESS_APPROVAL))
        | Q(cfo=request.user)
        | Q(accountant=request.user, status__in=(ClaimRequest.Status.PENDING_PAYMENT, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW, ClaimRequest.Status.COMPLETED))
        | Q(approval_records__actor=request.user)
    ).distinct()
    approval_claims = ClaimRequest.objects.filter(status__in=(ClaimRequest.Status.PENDING_BUDGET_APPROVAL, ClaimRequest.Status.PENDING_SUPERVISOR, ClaimRequest.Status.PENDING_EXECUTIVE, ClaimRequest.Status.PENDING_ACCOUNTING, ClaimRequest.Status.PENDING_PAYMENT, ClaimRequest.Status.PAID, ClaimRequest.Status.PENDING_CLEARANCE, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW, ClaimRequest.Status.PENDING_EXCESS_APPROVAL)).select_related("requester", "supervisor", "executive", "accounting_manager", "accountant", "cfo")
    # A completed request belongs on the accountant's dashboard too: they are
    # the person who performed the final clearance, even when someone else
    # submitted the request.
    completed_claims = claims.filter(status=ClaimRequest.Status.COMPLETED) if is_system_admin else ClaimRequest.objects.filter(
        Q(requester=request.user) | Q(accountant=request.user),
        status=ClaimRequest.Status.COMPLETED,
    ).distinct()
    completed_count = completed_claims.count()
    # Cancellation is also a final accounting action.  Keep it visible to
    # the person who actually cancelled the request, not only its requester.
    cancelled_claims = claims.filter(status=ClaimRequest.Status.CANCELLED) if is_system_admin else ClaimRequest.objects.filter(
        Q(requester=request.user)
        | Q(approval_records__actor=request.user, approval_records__action=ClaimApproval.Action.CANCELLED),
        status=ClaimRequest.Status.CANCELLED,
    ).distinct()
    cancelled_count = cancelled_claims.count()
    if not is_system_admin:
        approval_claims = approval_claims.filter(
            Q(budget_approvals__approver=request.user, budget_approvals__approved_at__isnull=True, status=ClaimRequest.Status.PENDING_BUDGET_APPROVAL)
            | Q(supervisor=request.user, status=ClaimRequest.Status.PENDING_SUPERVISOR)
            | Q(executive=request.user, status=ClaimRequest.Status.PENDING_EXECUTIVE)
            | Q(accounting_manager=request.user, status=ClaimRequest.Status.PENDING_ACCOUNTING, accounting_approved_at__isnull=True)
            | Q(accounting_manager=request.user, status=ClaimRequest.Status.PENDING_EXCESS_APPROVAL, excess_accounting_approved_at__isnull=True)
            | (Q(cfo=request.user) & pending_cfo_approval_q())
            | Q(accountant=request.user, status__in=(ClaimRequest.Status.PENDING_PAYMENT, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW))
            | Q(requester=request.user, status__in=(ClaimRequest.Status.PAID, ClaimRequest.Status.PENDING_CLEARANCE))
        )
    action_claims = list(approval_claims.distinct().order_by("-updated_at"))
    action_counts = {}
    for claim in action_claims:
        action_counts[claim.status] = action_counts.get(claim.status, 0) + 1
    pipeline_claims = ClaimRequest.objects.all() if is_system_admin else own_claims
    claim_counts = dict(pipeline_claims.values("status").annotate(total=Count("id")).values_list("status", "total"))
    # Active cards include claims assigned to this user, not only claims they requested.
    # This is the same visible set used by the claim list when "mine" is not selected.
    assigned_statuses = {
        ClaimRequest.Status.PENDING_BUDGET_APPROVAL,
        ClaimRequest.Status.PENDING_SUPERVISOR,
        ClaimRequest.Status.PENDING_PAYMENT,
        ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW,
        ClaimRequest.Status.PENDING_EXCESS_APPROVAL,
    }
    status_cards = [
        {"code": ClaimRequest.Status.DRAFT, "label": "แบบร่าง", "class": "draft"},
        {"code": ClaimRequest.Status.PENDING_BUDGET_APPROVAL, "label": "รออนุมัติ Budget", "class": "pending"},
        {"code": ClaimRequest.Status.PENDING_SUPERVISOR, "label": "รอผู้อนุมัติ", "class": "pending"},
        {"code": ClaimRequest.Status.PENDING_ACCOUNTING, "label": "รอบัญชีอนุมัติ", "class": "pending"},
        {"code": "CFO_APPROVAL", "label": "รอ CFO อนุมัติ", "class": "pending"},
        {"code": "CFO_ACKNOWLEDGEMENT", "label": "รอ CFO รับทราบ", "class": "pending"},
        {"code": ClaimRequest.Status.PENDING_PAYMENT, "label": "รอจ่ายเงิน", "class": "payment"},
        {"code": ClaimRequest.Status.PAID, "label": "รอยืนยันรับเงิน", "class": "payment"},
        {"code": ClaimRequest.Status.PENDING_CLEARANCE, "label": "รอเคลียร์", "class": "pending"},
        {"code": ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW, "label": "รอบัญชีตรวจ", "class": "pending"},
        {"code": ClaimRequest.Status.PENDING_EXCESS_APPROVAL, "label": "รออนุมัติส่วนต่าง", "class": "payment"},
        {"code": ClaimRequest.Status.COMPLETED, "label": "เสร็จสิ้น", "class": "completed"},
        {"code": ClaimRequest.Status.RETURNED, "label": "ส่งกลับแก้ไข", "class": "returned"},
        {"code": ClaimRequest.Status.REJECTED, "label": "ไม่อนุมัติ", "class": "rejected"},
        {"code": ClaimRequest.Status.CANCELLED, "label": "ยกเลิก", "class": "cancelled"},
    ]
    for card in status_cards:
        card["count"] = claim_counts.get(card["code"], 0)
        card["url"] = f"{reverse('claim_list')}?status={card['code']}" + ("" if is_system_admin else "&mine=1")
        if card["code"] in assigned_statuses:
            card["count"] = claims.filter(status=card["code"]).count()
            card["url"] = f"{reverse('claim_list')}?status={card['code']}"
        if card["code"] == ClaimRequest.Status.PENDING_ACCOUNTING:
            card["count"] = claims.filter(
                status=ClaimRequest.Status.PENDING_ACCOUNTING,
                accounting_approved_at__isnull=True,
            ).count()
            card["url"] = f"{reverse('claim_list')}?queue=accounting_approval"
        if card["code"] == "CFO_APPROVAL":
            card["count"] = claims.filter(pending_cfo_approval_q()).count()
            card["url"] = f"{reverse('claim_list')}?queue=cfo_approval"
        if card["code"] == "CFO_ACKNOWLEDGEMENT":
            card["count"] = len(pending_cfo_acknowledgement_ids(claims))
            card["url"] = f"{reverse('claim_list')}?queue=cfo_acknowledgement"
        if card["code"] == ClaimRequest.Status.COMPLETED:
            card["count"] = completed_count
            card["url"] = f"{reverse('claim_list')}?status={card['code']}" + ("" if is_system_admin else "&scope=completed")
        if card["code"] == ClaimRequest.Status.CANCELLED:
            card["count"] = cancelled_count
            card["url"] = f"{reverse('claim_list')}?status={card['code']}" + ("" if is_system_admin else "&scope=cancelled")
    has_workflow_role = request.user.groups.filter(name__in=(
        SUPERVISOR_APPROVER, ACCOUNTING_APPROVER, ACCOUNTANT, EXECUTIVE_APPROVER,
    )).exists() or configuration.cfo_id == request.user.pk or ClaimRequest.objects.filter(budget_approvals__approver=request.user, status=ClaimRequest.Status.PENDING_BUDGET_APPROVAL, budget_approvals__approved_at__isnull=True).exists()
    requester_summary = not is_system_admin and not has_workflow_role and not claims.exclude(requester=request.user).exists()
    if requester_summary:
        status_cards = [
            {
                "code": ClaimRequest.Status.DRAFT, "label": "แบบร่าง", "class": "draft",
                "count": claim_counts.get(ClaimRequest.Status.DRAFT, 0),
                "url": f"{reverse('claim_list')}?mine=1&status={ClaimRequest.Status.DRAFT}",
            },
            {
                "code": "in_progress", "label": "กำลังดำเนินการ", "class": "pending",
                "count": sum(claim_counts.get(status, 0) for status in REQUESTER_STATUS_GROUPS["in_progress"]),
                "url": f"{reverse('claim_list')}?mine=1&status_group=in_progress",
            },
        ]
        for status, label, card_class in (
            (ClaimRequest.Status.PAID, "รอยืนยันรับเงิน", "payment"),
            (ClaimRequest.Status.PENDING_CLEARANCE, "รอลงยอดใช้จริง", "pending"),
            (ClaimRequest.Status.COMPLETED, "เสร็จสิ้น", "completed"),
            (ClaimRequest.Status.RETURNED, "ส่งกลับแก้ไข", "returned"),
        ):
            status_cards.append({
                "code": status, "label": label, "class": card_class,
                "count": claim_counts.get(status, 0),
                "url": f"{reverse('claim_list')}?mine=1&status={status}",
            })
        status_cards.append({
            "code": "unsuccessful", "label": "ไม่อนุมัติ/ยกเลิก", "class": "rejected",
            "count": sum(claim_counts.get(status, 0) for status in REQUESTER_STATUS_GROUPS["unsuccessful"]),
            "url": f"{reverse('claim_list')}?mine=1&status_group=unsuccessful",
        })
    actionable_claims = action_claims[:5]
    recent_claims = claims.select_related("requester").order_by("-updated_at")[:5]
    return render(request, "core/dashboard.html", {
        "can_manage_users": is_system_admin,
        "is_system_admin": is_system_admin,
        "my_claim_count": claims.count(),
        "own_claim_count": own_claims.count(),
        "draft_count": pipeline_claims.filter(status=ClaimRequest.Status.DRAFT).count(),
        "approval_count": len(action_claims),
        "completed_count": completed_count,
        "completed_list_url": f"{reverse('claim_list')}?status={ClaimRequest.Status.COMPLETED}" + ("" if is_system_admin else "&scope=completed"),
        "status_cards": status_cards,
        "requester_summary": requester_summary,
        "signature_required": configuration.require_signature_on_first_login,
        "actionable_claims": actionable_claims,
        "recent_claims": recent_claims,
        **requester_tracking(request.user),
    })


@login_required
@never_cache
def dashboard_claim_tracker(request):
    return render(request, "core/_claim_tracker.html", requester_tracking(request.user))


@login_required
def mark_notifications_read(request):
    if request.method == "POST":
        request.user.notifications.filter(is_read=False).update(is_read=True)
    return redirect(request.POST.get("next") or "dashboard")


@login_required
def notification_open(request, notification_id):
    """Mark one notification read only when its detail link is opened."""
    notification = get_object_or_404(Notification, pk=notification_id, recipient=request.user)
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=["is_read"])
    return redirect(notification.link or "dashboard")


@login_required
def notification_feed(request):
    """Small polling endpoint for the global live notification bell."""
    unread = request.user.notifications.filter(is_read=False).order_by("-created_at")
    notifications = [
        {
            "id": notification.pk,
            "title": notification.title,
            "message": notification.message,
            "link": reverse("notification_open", kwargs={"notification_id": notification.pk}),
            "created_at": timezone.localtime(notification.created_at).strftime("%d/%m %H:%M"),
        }
        for notification in unread[:5]
    ]
    response = JsonResponse({"count": unread.count(), "notifications": notifications})
    response["Cache-Control"] = "no-store"
    return response


@login_required
def system_settings(request):
    if not can_manage_users(request.user):
        return redirect("dashboard")
    form = SystemSettingsForm(request.POST or None, instance=get_system_settings())
    if request.method == "POST" and form.is_valid():
        form.save()
        AdminAuditLog.objects.create(actor=request.user, action="UPDATE_SYSTEM_SETTINGS", target="System settings")
        messages.success(request, "บันทึกการตั้งค่าระบบเรียบร้อยแล้ว")
        return redirect("system_settings")
    return render(request, "core/system_settings.html", {"form": form})


@login_required
def system_overview(request):
    """Read-only operational monitor for Super Admins."""
    if not can_manage_users(request.user):
        return redirect("dashboard")

    today = timezone.localdate()
    date_to = _monitor_date(request.GET.get("date_to"), today)
    date_from = _monitor_date(request.GET.get("date_from"), today - timedelta(days=29))
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    if (date_to - date_from).days > 180:
        date_from = date_to - timedelta(days=180)

    selected_requester = request.GET.get("requester", "").strip()
    selected_status = request.GET.get("status", "").strip()
    claims = ClaimRequest.objects.filter(created_at__date__range=(date_from, date_to))
    if selected_requester.isdigit() and User.objects.filter(pk=selected_requester).exists():
        claims = claims.filter(requester_id=selected_requester)
    else:
        selected_requester = ""
    valid_statuses = {code for code, _, _ in MONITOR_STATUS_META}
    if selected_status in valid_statuses:
        claims = claims.filter(status=selected_status)
    else:
        selected_status = ""

    claim_count = claims.count()
    requested_total = claims.aggregate(total=Sum("requested_total"))["total"] or 0
    actual_total = claims.aggregate(total=Sum("actual_total"))["total"] or 0
    completed_count = claims.filter(status=ClaimRequest.Status.COMPLETED).count()
    completion_rate = round((completed_count / claim_count) * 100) if claim_count else 0

    daily_counts = dict(claims.values("created_at__date").annotate(total=Count("id")).values_list("created_at__date", "total"))
    max_daily_count = max(daily_counts.values(), default=1)
    daily_series = []
    day = date_from
    while day <= date_to:
        count = daily_counts.get(day, 0)
        daily_series.append({"label": day.strftime("%d/%m"), "full_label": day.strftime("%d/%m/%Y"), "count": count, "height": max(5, round((count / max_daily_count) * 100)) if count else 3})
        day += timedelta(days=1)

    status_counts = dict(claims.values("status").annotate(total=Count("id")).values_list("status", "total"))
    status_breakdown, gradient_parts, position = [], [], 0
    for code, label, color in MONITOR_STATUS_META:
        count = status_counts.get(code, 0)
        if not count:
            continue
        percentage = (count / claim_count) * 100 if claim_count else 0
        status_breakdown.append({"label": label, "count": count, "color": color, "percentage": round(percentage)})
        gradient_parts.append(f"{color} {position:.2f}% {position + percentage:.2f}%")
        position += percentage
    status_gradient = ", ".join(gradient_parts) if gradient_parts else "#e5edf6 0 100%"

    current_queue_codes = (ClaimRequest.Status.PENDING_SUPERVISOR, ClaimRequest.Status.PENDING_EXECUTIVE, ClaimRequest.Status.PENDING_ACCOUNTING, ClaimRequest.Status.PENDING_PAYMENT, ClaimRequest.Status.PAID, ClaimRequest.Status.PENDING_CLEARANCE, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW, ClaimRequest.Status.PENDING_EXCESS_APPROVAL)
    queue_counts = dict(ClaimRequest.objects.filter(status__in=current_queue_codes).values("status").annotate(total=Count("id")).values_list("status", "total"))
    current_queue_total = sum(queue_counts.values())
    queue_breakdown = [{"label": label, "count": queue_counts.get(code, 0), "color": color} for code, label, color in MONITOR_STATUS_META if code in current_queue_codes and queue_counts.get(code, 0)]

    top_units = list(claims.exclude(business_unit="").values("business_unit").annotate(count=Count("id"), requested_total=Sum("requested_total"), actual_total=Sum("actual_total")).order_by("-requested_total", "business_unit")[:5])
    for unit in top_units:
        unit["actual_total"] = unit["actual_total"] or 0
    top_requesters = list(claims.values("requester__username", "requester__first_name", "requester__last_name").annotate(count=Count("id"), requested_total=Sum("requested_total"), actual_total=Sum("actual_total")).order_by("-requested_total", "requester__username")[:5])
    for requester in top_requesters:
        requester["name"] = " ".join(filter(None, (requester.pop("requester__first_name"), requester.pop("requester__last_name")))) or requester.pop("requester__username")
        requester["actual_total"] = requester["actual_total"] or 0

    audit_logs = AdminAuditLog.objects.filter(created_at__date__range=(date_from, date_to)).select_related("actor")[:8]
    requester_users = User.objects.filter(groups__name=REQUESTER, is_active=True).distinct()
    missing_workflow_count = sum(not all((user.supervisor_id, user.accounting_manager_id, user.accountant_id)) for user in requester_users)
    configuration = get_system_settings()
    try:
        connection.ensure_connection()
        database_ok = connection.is_usable()
    except Exception:
        database_ok = False
    return render(request, "core/system_overview.html", {
        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "selected_requester": selected_requester, "selected_status": selected_status,
        "requesters": User.objects.filter(claim_requests__isnull=False).distinct().order_by("first_name", "last_name", "username"), "statuses": [(code, label) for code, label, _ in MONITOR_STATUS_META],
        "claim_count": claim_count, "requested_total": requested_total, "actual_total": actual_total, "completed_count": completed_count, "completion_rate": completion_rate,
        "daily_series": daily_series, "status_breakdown": status_breakdown, "status_gradient": status_gradient, "current_queue_total": current_queue_total, "queue_breakdown": queue_breakdown,
        "top_units": top_units, "top_requesters": top_requesters, "audit_logs": audit_logs, "active_user_count": User.objects.filter(is_active=True).count(),
        "ad_user_count": User.objects.filter(is_active=True, is_ad_auth=True).count(), "telegram_user_count": User.objects.filter(is_active=True, telegram_notifications_enabled=True).exclude(telegram_user_id__isnull=True).exclude(telegram_user_id="").count(),
        "missing_workflow_count": missing_workflow_count, "configuration": configuration, "web_health": get_web_health(), "database_ok": database_ok,
    })


@login_required
def admin_operations(request):
    if not can_manage_users(request.user):
        return redirect("dashboard")
    configuration = get_system_settings()
    test_result = ""
    if request.method == "POST":
        action = request.POST.get("action") or "unknown"
        try:
            if action == "unknown":
                raise ValueError("ไม่พบประเภทการทดสอบ กรุณาลองกดปุ่มทดสอบอีกครั้ง")
            if action == "database":
                connection.ensure_connection()
                test_result = "ฐานข้อมูลเชื่อมต่อได้"
            elif action == "ad":
                target = urlparse(configuration.ad_gateway_url or settings.AD_GATEWAY_URL)
                if not target.hostname:
                    raise ValueError("ยังไม่ได้ตั้งค่า AD Gateway URL")
                with socket.create_connection((target.hostname, target.port or 80), timeout=5):
                    pass
                test_result = "เชื่อมต่อ AD Gateway ได้"
            elif action == "telegram":
                test_result = "เชื่อมต่อ Telegram Bot ได้" if telegram_api_request("getMe", {}) else "เชื่อมต่อ Telegram Bot ไม่ได้"
            elif action == "telegram_webhook":
                if not configuration.telegram_enabled:
                    raise ValueError("Telegram ถูกปิดการใช้งานอยู่ กรุณาเปิดใช้งานก่อน")
                if not request.user.telegram_user_id:
                    raise ValueError("ผู้ดูแลคนนี้ยังไม่ได้ผูก Telegram User ID")
                if not telegram_api_request("getWebhookInfo", {}):
                    raise ValueError("ไม่สามารถตรวจสอบ Webhook ของ Telegram ได้")
                check = TelegramWebhookCheck.objects.create(token=uuid4().hex, requested_by=request.user)
                sent = send_telegram_message(
                    request.user.telegram_user_id,
                    build_telegram_text("ตรวจสอบ Telegram Webhook", "กดปุ่มด้านล่างเพื่อยืนยันว่า Bot รับ callback จาก Telegram ได้จริง"),
                    {"inline_keyboard": [[{"text": "✅ ยืนยัน Webhook", "callback_data": f"health:{check.token}"}]]},
                )
                if not sent:
                    check.delete()
                    raise ValueError("ส่งข้อความทดสอบไปยัง Telegram ไม่สำเร็จ")
                test_result = "ส่งปุ่มยืนยัน Webhook ไปยัง Telegram ของผู้ดูแลแล้ว"
            AdminAuditLog.objects.create(actor=request.user, action=f"TEST_{action.upper()}", target="System status", detail=test_result)
        except Exception as exc:
            labels = {"database": "ฐานข้อมูล", "ad": "AD Gateway", "telegram": "Telegram Bot", "telegram_webhook": "Telegram Webhook"}
            test_result = f"ทดสอบ {labels.get(action, 'ระบบ')} ไม่สำเร็จ: {exc}"
        messages.info(request, test_result)
        return redirect("admin_operations")
    try:
        connection.ensure_connection()
        database_ok = True
    except Exception:
        database_ok = False
    incomplete_users = []
    for user in User.objects.filter(groups__name=REQUESTER, is_active=True).select_related(
        'supervisor', 'accounting_manager', 'accountant', 'alternate_payer',
    ).prefetch_related('groups').distinct().order_by("username"):
        missing = []
        # Executive is legacy-only; current requests use supervisor -> accounting -> CFO.
        for field, label in (("supervisor", "ผู้อนุมัติ"), ("accounting_manager", "ผู้จัดการบัญชี")):
            actor = getattr(user, field)
            if actor is None:
                missing.append(label)
            elif not actor.is_active:
                missing.append(f'{label} (บัญชีปิดใช้งาน)')
        if user.claim_payer() is None:
            missing.append('เจ้าหน้าที่บัญชี / ผู้จ่ายสำรอง')
        if missing:
            incomplete_users.append({"user": user, "missing": ", ".join(missing), "missing_fields": missing})
    audit_logs = AdminAuditLog.objects.select_related("actor")
    audit_query = request.GET.get("audit_q", "").strip()
    audit_action = request.GET.get("audit_action", "").strip()
    audit_actor = request.GET.get("audit_actor", "")
    audit_date_from = request.GET.get("audit_date_from", "")
    audit_date_to = request.GET.get("audit_date_to", "")
    if audit_query:
        search_key = _audit_search_key(audit_query)
        matching_actions = [action for action, label in AUDIT_ACTION_LABELS.items() if search_key in _audit_search_key(label) or search_key in _audit_search_key(action)]
        audit_logs = audit_logs.filter(Q(action__icontains=audit_query) | Q(action__in=matching_actions) | Q(target__icontains=audit_query) | Q(detail__icontains=audit_query) | Q(actor__username__icontains=audit_query))
    if audit_action:
        action_key = _audit_search_key(audit_action)
        matching_actions = [action for action, label in AUDIT_ACTION_LABELS.items() if action_key in _audit_search_key(label) or action_key in _audit_search_key(action)]
        audit_logs = audit_logs.filter(Q(action__in=matching_actions) | Q(action__icontains=audit_action))
    if audit_actor:
        audit_logs = audit_logs.filter(Q(actor__username__icontains=audit_actor) | Q(actor__first_name__icontains=audit_actor) | Q(actor__last_name__icontains=audit_actor))
    if audit_date_from:
        audit_logs = audit_logs.filter(created_at__date__gte=audit_date_from)
    if audit_date_to:
        audit_logs = audit_logs.filter(created_at__date__lte=audit_date_to)
    audit_logs, page_obj, page_size, query_params = paginate(request, audit_logs)
    action_codes = AdminAuditLog.objects.order_by("action").values_list("action", flat=True).distinct()
    audit_actions = [{"code": action, "label": AUDIT_ACTION_LABELS.get(action, action.replace("_", " ").title())} for action in action_codes]
    return render(request, "core/admin_operations.html", {"database_ok": database_ok, "configuration": configuration, "audit_logs": audit_logs, "users": [], "claims": [], "incomplete_users": incomplete_users, "test_result": test_result, "audit_query": audit_query, "audit_action": audit_action, "audit_actor": audit_actor, "audit_date_from": audit_date_from, "audit_date_to": audit_date_to, "audit_actions": audit_actions, "audit_actors": User.objects.filter(adminauditlog__isnull=False).distinct().order_by("username"), "page_obj": page_obj, "page_size": page_size, "query_params": query_params})
