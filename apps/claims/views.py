import json
import mimetypes
from io import BytesIO
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.clickjacking import xframe_options_sameorigin

from apps.accounts.models import User
from .forms import ClaimItemFormSet, ClaimRequestForm
from .telegram_totals import daily_approval_breakdown, telegram_message_parts
from .policies import expense_month_error
from .references import can_view_references, reference_rows, exposure_snapshot
from .models import (
    ClaimApproval, ClaimAttachment, ClaimBudgetApproval, ClaimPayment, ClaimRequest, REQUESTER_STATUS_GROUPS,
    TelegramApprovalPrompt, TelegramCallbackReceipt, ClaimDocumentSequence,
    REQUESTER_STATUS_GROUP_LABELS, pending_cfo_acknowledgement_ids, pending_cfo_approval_q,
)
from apps.accounts.roles import ACCOUNTANT, SUPER_ADMIN
from apps.core.integrations import public_base_url, telegram_secret
from apps.core.models import Budget, Notification, TelegramWebhookCheck
from apps.core.audit import log_event
from apps.core.pagination import paginate
from apps.core.system_settings import get_system_settings
from apps.core.telegram import (
    answer_telegram_callback,
    build_telegram_text,
    remove_telegram_callback_keyboard,
    replace_telegram_callback_keyboard,
    send_telegram_group_notification,
    send_telegram_message,
    send_telegram_notification,
)
from .reports import build_claim_report


ALLOWED_ATTACHMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


def is_system_admin(user):
    return user.is_superuser or user.has_role(SUPER_ADMIN)


def can_export_claims(user):
    return is_system_admin(user) or user.has_role(ACCOUNTANT)


def attachment_errors(uploads, existing_count=0):
    configuration = get_system_settings()
    errors = []
    if len(uploads) + existing_count > configuration.max_attachment_count:
        errors.append(f"แนบไฟล์ได้รวมไม่เกิน {configuration.max_attachment_count} ไฟล์ต่อคำขอ")
    for upload in uploads:
        extension = upload.name[upload.name.rfind("."):].lower() if "." in upload.name else ""
        if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
            errors.append(f"{upload.name}: รองรับเฉพาะ PDF, JPG และ PNG")
        elif upload.size > configuration.max_attachment_size_mb * 1024 * 1024:
            errors.append(f"{upload.name}: ไฟล์ต้องมีขนาดไม่เกิน {configuration.max_attachment_size_mb} MB")
    return errors


def payment_input_errors(method, slip):
    if method not in ClaimPayment.Method.values:
        return ["กรุณาเลือกวิธีการจ่ายเงิน"]
    if method == ClaimPayment.Method.TRANSFER and not slip:
        return ["การโอนเงินต้องแนบสลิปก่อนบันทึกจ่ายเงิน"]
    if method == ClaimPayment.Method.CASH and slip:
        return ["จ่ายเงินสดไม่ต้องแนบสลิป กรุณานำไฟล์ออก"]
    return attachment_errors([slip]) if slip else []


def claim_item_choices(requester):
    return {
        "budget_options": list(Budget.objects.exclude(department="").values("department", "name").order_by("department", "name")),
        "budget_manager_options": [
            {"department": user.department, "id": user.pk, "name": user.get_full_name() or user.username}
            for user in User.objects.filter(is_active=True, position_level=User.PositionLevel.MANAGER).exclude(pk=requester.pk).exclude(department="").order_by("department", "first_name", "username")
        ],
        "requester_department": requester.department,
    }


def owned_claim_or_404(user, claim_id):
    try:
        claims = ClaimRequest.objects.prefetch_related("items", "attachments")
        if not is_system_admin(user):
            claims = claims.filter(requester=user)
        return claims.get(pk=claim_id)
    except ClaimRequest.DoesNotExist:
        raise Http404("ไม่พบคำขอเบิก")


def visible_claim_or_404(user, claim_id):
    try:
        claim = (
            ClaimRequest.objects.select_related(
                "requester", "supervisor", "executive", "accounting_manager", "accountant", "cfo"
            )
            .prefetch_related("items", "attachments__uploaded_by", "payments__paid_by", "approval_records__actor")
            .get(pk=claim_id)
        )
    except ClaimRequest.DoesNotExist:
        raise Http404("ไม่พบคำขอเบิก")
    if is_system_admin(user) or (
        claim.requester_id == user.id
        or claim.can_approve(user)
        or claim.budget_approvals.filter(approver_id=user.id).exists()
        or claim.cfo_id == user.id
        or claim.accountant_id == user.id
        or any(record.actor_id == user.id for record in claim.approval_records.all())
    ):
        return claim
    raise Http404("ไม่พบคำขอเบิก")


def can_view_claim_payments(user, claim):
    return is_system_admin(user) or user.pk in {
        claim.requester_id, claim.accountant_id, claim.accounting_manager_id, claim.cfo_id,
    }


def add_approval_record(claim, actor, action, status_before, comment=""):
    ClaimApproval.objects.create(
        claim_request=claim, actor=actor, action=action,
        status_before=status_before, status_after=claim.status, comment=comment,
    )


def pending_action_title(claim):
    if claim.status == ClaimRequest.Status.PENDING_BUDGET_APPROVAL:
        return "รอผู้อนุมัติ Budget"
    if claim.status == ClaimRequest.Status.PENDING_EXECUTIVE:
        return "รอผู้บริหารอนุมัติ"
    if claim.status == ClaimRequest.Status.PENDING_ACCOUNTING:
        return claim.workflow_status_display if claim.accounting_approved_at else "รอผู้จัดการบัญชีอนุมัติ"
    if claim.status == ClaimRequest.Status.PENDING_EXCESS_APPROVAL:
        return claim.workflow_status_display
    if claim.status == ClaimRequest.Status.PENDING_PAYMENT:
        return "รอเจ้าหน้าที่บัญชีจ่ายเงิน"
    if claim.status == ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW:
        return "รอบัญชีตรวจยอดใช้จริง"
    return "มีคำขอรอดำเนินการ"


@transaction.atomic
def approve_claim_step(claim, actor):
    """Advance a request, requiring accounting approval before CFO approval."""
    locked = ClaimRequest.objects.select_for_update(of=('self',)).select_related("requester").get(pk=claim.pk)
    if locked.status != claim.status or (not is_system_admin(actor) and not locked.can_approve(actor)):
        return False
    if locked.status == ClaimRequest.Status.PENDING_BUDGET_APPROVAL:
        pending = locked.budget_approvals.filter(approved_at__isnull=True)
        approver_id = actor.pk if not is_system_admin(actor) else pending.order_by("pk").values_list("approver_id", flat=True).first()
        if not approver_id or not pending.filter(approver_id=approver_id).exists():
            return False
        pending.filter(approver_id=approver_id).update(approved_at=timezone.now(), acted_by=actor)
        if not pending.exists():
            locked.status = ClaimRequest.Status.PENDING_SUPERVISOR
    elif locked.status == ClaimRequest.Status.PENDING_SUPERVISOR:
        locked.status = ClaimRequest.Status.PENDING_ACCOUNTING
    elif locked.status == ClaimRequest.Status.PENDING_EXECUTIVE:
        locked.status = ClaimRequest.Status.PENDING_ACCOUNTING
    elif locked.status == ClaimRequest.Status.PENDING_ACCOUNTING:
        if actor.pk == locked.accounting_manager_id or (is_system_admin(actor) and not locked.accounting_approved_at):
            locked.accounting_approved_at = timezone.now()
        elif actor.pk == locked.cfo_id or (is_system_admin(actor) and not locked.cfo_approved_at):
            locked.cfo_approved_at = timezone.now()
        if locked.accounting_approved_at and (not locked.requires_cfo_approval or locked.cfo_approved_at):
            locked.status = ClaimRequest.Status.PENDING_PAYMENT
    elif locked.status == ClaimRequest.Status.PENDING_EXCESS_APPROVAL:
        if actor.pk == locked.accounting_manager_id or (is_system_admin(actor) and not locked.excess_accounting_approved_at):
            locked.excess_accounting_approved_at = timezone.now()
        elif actor.pk == locked.cfo_id or (is_system_admin(actor) and not locked.excess_cfo_approved_at):
            locked.excess_cfo_approved_at = timezone.now()
        if locked.excess_accounting_approved_at and (not locked.requires_cfo_excess_approval or locked.excess_cfo_approved_at):
            locked.status = ClaimRequest.Status.PENDING_PAYMENT
    fields = ['status', 'accounting_approved_at', 'cfo_approved_at', 'excess_accounting_approved_at', 'excess_cfo_approved_at']
    locked.save(update_fields=[*fields, 'updated_at'])
    for field in fields:
        setattr(claim, field, getattr(locked, field))
    return True


@transaction.atomic
def confirm_receipt_step(claim, actor, payment_event_id=None):
    """Confirm a receipt once, even when web, Telegram and scheduler race."""
    locked = ClaimRequest.objects.select_for_update(of=('self',)).select_related("requester").get(pk=claim.pk)
    if locked.status != ClaimRequest.Status.PAID or (not is_system_admin(actor) and locked.requester_id != actor.pk):
        return None
    event = locked.latest_payment_event
    supplemental = locked.latest_payment_is_supplemental
    if payment_event_id is not None and (event is None or str(event.pk) != str(payment_event_id)):
        return None
    if supplemental and payment_event_id is None:
        return None
    amount = locked.receipt_amount
    locked.status = (
        ClaimRequest.Status.COMPLETED if locked.receipt_closes_claim
        else ClaimRequest.Status.PENDING_CLEARANCE
    )
    locked.save(update_fields=["status", "updated_at"])
    comment = f"ยืนยันรับเงิน{'ส่วนต่าง' if supplemental else ''} {amount:,.2f} บาท"
    add_approval_record(locked, actor, ClaimApproval.Action.RECEIVED, ClaimRequest.Status.PAID, comment)
    log_event(actor, "CONFIRM_RECEIPT", locked.document_number or f"คำขอ #{locked.pk}", comment)
    return locked


@transaction.atomic
def acknowledge_cfo(claim, actor, stage):
    locked = ClaimRequest.objects.select_for_update(of=('self',)).select_related("requester").get(pk=claim.pk)
    if not locked.can_cfo_acknowledge(actor, stage):
        return False
    field = "cfo_excess_acknowledged_at" if stage == "excess" else "cfo_acknowledged_at"
    setattr(locked, field, timezone.now())
    locked.save(update_fields=[field, "updated_at"])
    add_approval_record(
        locked, actor, ClaimApproval.Action.ACKNOWLEDGED, locked.status,
        "CFO รับทราบยอดใช้เกิน" if stage == "excess" else "CFO รับทราบคำขอ",
    )
    log_event(actor, "ACKNOWLEDGE_CLAIM", locked.document_number or f"คำขอ #{locked.pk}", stage)
    title = "CFO รับทราบยอดใช้เกินแล้ว" if stage == "excess" else "CFO รับทราบคำขอแล้ว"
    message = (
        f"เลขที่: {locked.document_number or f'คำขอ #{locked.pk}'}\n"
        f"CFO: {actor.get_full_name() or actor.username}\n"
        f"สถานะ: {locked.get_status_display()}"
    )
    transaction.on_commit(lambda: send_telegram_group_notification(title, message, icon="👀"))
    return True


def notify_next_approvers(claim, previous_status=None):
    if claim.status == ClaimRequest.Status.PENDING_BUDGET_APPROVAL:
        if previous_status != ClaimRequest.Status.PENDING_BUDGET_APPROVAL:
            for approval in claim.budget_approvals.filter(approved_at__isnull=True).select_related("approver"):
                notify(approval.approver, "รออนุมัติ Budget", f"คำขอใช้ Budget ของแผนก {approval.department} รอให้คุณพิจารณาก่อนส่งต่อผู้อนุมัติของผู้ขอ", claim)
        return
    if claim.status == ClaimRequest.Status.PENDING_EXCESS_APPROVAL:
        recipient = claim.current_approver()
        if recipient:
            message = f'ยอดใช้จริง {claim.actual_total:,.2f} บาท · ส่วนต่าง {claim.settlement_amount:,.2f} บาท'
            if claim.excess_accounting_approved_at:
                message = 'ผู้จัดการบัญชีอนุมัติส่วนต่างแล้ว กรุณาพิจารณาอนุมัติขั้น CFO\n' + message
            notify(recipient, pending_action_title(claim), message, claim)
        return
    if claim.status == ClaimRequest.Status.PENDING_ACCOUNTING:
        if not claim.accounting_approved_at and claim.accounting_manager:
            message = "บัญชีแก้ยอดก่อนจ่ายแล้ว กรุณาอนุมัติยอดใหม่ก่อนส่ง CFO" if claim.corrected_cfo_required else "คำขอรอการอนุมัติจากคุณ"
            notify(claim.accounting_manager, "รอผู้จัดการบัญชีอนุมัติ", message, claim)
        elif claim.accounting_approved_at and claim.requires_cfo_approval and claim.cfo and not claim.cfo_approved_at:
            notify(claim.cfo, "รอ CFO อนุมัติ", "ผู้จัดการบัญชีอนุมัติแล้ว กรุณาพิจารณาอนุมัติขั้น CFO", claim)
        return
    if claim.status == ClaimRequest.Status.PENDING_PAYMENT and claim.cfo:
        if previous_status == ClaimRequest.Status.PENDING_ACCOUNTING and not claim.requires_cfo_approval and not claim.cfo_acknowledged_at:
            notify(claim.cfo, "CFO รับทราบคำขอ", "ผู้จัดการบัญชีอนุมัติแล้ว กรุณากดรับทราบคำขอ (ไม่ต้องอนุมัติ)", claim, cfo_ack_stage="initial")
        elif previous_status == ClaimRequest.Status.PENDING_EXCESS_APPROVAL and claim.has_excess and not claim.requires_cfo_excess_approval and not claim.cfo_excess_acknowledged_at:
            notify(claim.cfo, "CFO รับทราบยอดใช้เกิน", f"ผู้จัดการบัญชีอนุมัติส่วนต่าง {claim.settlement_amount:,.2f} บาทแล้ว กรุณากดรับทราบ (ไม่ต้องอนุมัติ)", claim, cfo_ack_stage="excess")
    recipient = claim.current_approver()
    if recipient:
        notify(recipient, pending_action_title(claim), f"คำขอของ {claim.requester.get_full_name() or claim.requester.username} รอขั้นตอนของคุณ", claim)


@transaction.atomic
def assign_document_number(claim):
    """Assign a permanent monthly number using the configured location prefix."""
    if claim.document_number:
        return
    document_date = timezone.localdate()
    if claim.work_location not in ('HQ', 'BRANCH'):
        raise ValueError('กรุณาเลือกสถานที่เป็นสำนักงานใหญ่หรือกาญจนบุรีก่อนออกเลขเอกสาร')
    configuration = get_system_settings()
    prefix = configuration.head_office_document_prefix if claim.work_location == 'HQ' else configuration.branch_document_prefix
    counter, _ = ClaimDocumentSequence.objects.get_or_create(prefix=prefix, period=document_date.strftime('%Y%m'))
    counter = ClaimDocumentSequence.objects.select_for_update().get(pk=counter.pk)
    number = counter.last_number
    while True:
        number += 1
        if number > 9999:
            raise ValueError('เลขเอกสารเดือนนี้ครบ 9999 รายการแล้ว กรุณาติดต่อผู้ดูแลระบบ')
        document = f'{prefix}-{(document_date.year + 543) % 100:02d}{document_date:%m}{number:04d}'
        if not ClaimRequest.objects.filter(document_number=document).exists():
            break
    counter.last_number = number
    counter.save(update_fields=['last_number'])
    claim.document_number = document


def thai_baht_text(amount):
    """Return a compact Thai baht spelling for the printed document."""
    amount = Decimal(amount).quantize(Decimal("0.01"))
    integer, satang = divmod(int(amount * 100), 100)
    digits = ("ศูนย์", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า")
    places = ("", "สิบ", "ร้อย", "พัน", "หมื่น", "แสน")

    def spell(number):
        if number == 0:
            return ""
        if number >= 1_000_000:
            return spell(number // 1_000_000) + "ล้าน" + spell(number % 1_000_000)
        result = ""
        text = str(number)
        for index, character in enumerate(text):
            digit = int(character)
            position = len(text) - index - 1
            if not digit:
                continue
            if position == 1 and digit == 1:
                result += "สิบ"
            elif position == 1 and digit == 2:
                result += "ยี่สิบ"
            elif position == 0 and digit == 1 and len(text) > 1:
                result += "เอ็ด"
            else:
                result += digits[digit] + places[position]
        return result

    baht = spell(integer) or "ศูนย์"
    return f"{baht}บาทถ้วน" if not satang else f"{baht}บาท{spell(satang)}สตางค์"


def notify(user, title, message, claim, cfo_ack_stage=None):
    status_icons = {
        ClaimRequest.Status.DRAFT: "📝", ClaimRequest.Status.PENDING_SUPERVISOR: "⏳",
        ClaimRequest.Status.PENDING_BUDGET_APPROVAL: "⏳",
        ClaimRequest.Status.PENDING_EXECUTIVE: "⏳", ClaimRequest.Status.PENDING_ACCOUNTING: "⏳",
        ClaimRequest.Status.PENDING_PAYMENT: "💸", ClaimRequest.Status.PAID: "💳",
        ClaimRequest.Status.PENDING_CLEARANCE: "📋", ClaimRequest.Status.PENDING_EXCESS_APPROVAL: "⚠️",
        ClaimRequest.Status.COMPLETED: "✅", ClaimRequest.Status.RETURNED: "↩️",
        ClaimRequest.Status.REJECTED: "⛔", ClaimRequest.Status.CANCELLED: "🚫",
    }
    status_icon = status_icons.get(claim.status, "🔔")
    link = reverse("claim_detail", kwargs={"claim_id": claim.id})
    Notification.objects.create(recipient=user, title=title, message=message, link=link)
    document_number = claim.document_number or "รอออกเลขเอกสาร"
    approval_records = list(
        claim.approval_records.select_related("actor").order_by("-created_at", "-pk")
    )
    latest_submission = next(
        (index for index, record in enumerate(approval_records) if record.action in (ClaimApproval.Action.SUBMITTED, ClaimApproval.Action.EXCESS_SUBMITTED, ClaimApproval.Action.RETURNED)
         or (record.action == ClaimApproval.Action.AMOUNT_CORRECTED and record.status_after == ClaimRequest.Status.PENDING_ACCOUNTING)),
        len(approval_records),
    )
    approval_roles = {
        ClaimRequest.Status.PENDING_BUDGET_APPROVAL: "ผู้อนุมัติ Budget",
        ClaimRequest.Status.PENDING_SUPERVISOR: "ผู้อนุมัติ",
        ClaimRequest.Status.PENDING_EXECUTIVE: "ผู้บริหาร",
        ClaimRequest.Status.PENDING_ACCOUNTING: "ผู้จัดการบัญชี",
        ClaimRequest.Status.PENDING_EXCESS_APPROVAL: "ผู้จัดการบัญชี (ส่วนต่าง)",
    }
    approved_by = [
        f"{record.actor.get_full_name() or record.actor.username} ({'CFO' if record.actor_id == claim.cfo_id and record.status_before in (ClaimRequest.Status.PENDING_ACCOUNTING, ClaimRequest.Status.PENDING_EXCESS_APPROVAL) else approval_roles.get(record.status_before, 'ผู้อนุมัติ')})"
        for record in reversed(approval_records[:latest_submission])
        if record.action == ClaimApproval.Action.APPROVED
    ]
    approval_summary = f"ผ่านการอนุมัติแล้ว: {', '.join(approved_by)}\n" if approved_by else ""
    items = list(claim.items.all())
    completed = claim.status == ClaimRequest.Status.COMPLETED
    settlement_stage = completed or (
        claim.request_type == ClaimRequest.RequestType.ADVANCE
        and claim.status in (ClaimRequest.Status.PENDING_CLEARANCE, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW,
            ClaimRequest.Status.PENDING_EXCESS_APPROVAL, ClaimRequest.Status.PENDING_PAYMENT,
            ClaimRequest.Status.PAID)
        and claim.actual_total is not None
    )
    show_actual = settlement_stage and claim.actual_total is not None
    budget_departments = list(claim.budget_approvals.filter(
        approver_id=user.pk, approved_at__isnull=True,
    ).values_list("department", flat=True)) if claim.status == ClaimRequest.Status.PENDING_BUDGET_APPROVAL else []
    if budget_departments:
        # Budget owners approve only their assigned lines, not the entire claim.
        items = [item for item in items if item.department in budget_departments and item.budget_approver_id == user.pk]
        requested_amount = sum((item.requested_amount for item in items), Decimal('0.00'))
        amount_label = f"ยอดขออนุมัติ Budget (แผนก {', '.join(budget_departments)})"
    else:
        requested_amount = claim.requested_total
        amount_label = "ยอดขอเบิก"
    if show_actual:
        requested_amount = claim.actual_total
        amount_label = "ยอดใช้จริงรวม"
    item_lines = []
    for item in items:
        if settlement_stage:
            item_amount = f"{item.actual_amount:,.2f} บาท" if item.actual_amount is not None else "ยังไม่ระบุยอดใช้จริง"
        else:
            item_amount = f"{item.requested_amount:,.2f} บาท"
        item_lines.append(f"• {item.description}: {item_amount} | Budget {item.budget or claim.budget or '-'}"
                          f" | แผนก {item.department or claim.department or '-'}")
    item_summary = "\n".join(item_lines)
    amount_context = ''
    if show_actual:
        payments = list(claim.payments.all())
        supplemental_paid = sum((payment.amount for payment in payments if payment.kind == ClaimPayment.Kind.SUPPLEMENTAL), Decimal('0.00'))
        if not supplemental_paid and any(record.action == ClaimApproval.Action.SUPPLEMENTAL_PAID for record in approval_records):
            supplemental_paid = max(claim.actual_total - claim.requested_total, Decimal('0.00'))
        # Legacy claims can have approval events but no initial payment ledger row.
        initial_paid = sum((payment.amount for payment in payments if payment.kind == ClaimPayment.Kind.INITIAL), Decimal('0.00'))
        funded_total = (initial_paid if any(payment.kind == ClaimPayment.Kind.INITIAL for payment in payments) else claim.requested_total) + supplemental_paid
        returned = max(funded_total - claim.actual_total, Decimal('0.00'))
        still_due = max(claim.actual_total - funded_total, Decimal('0.00'))
        funding_label = 'จ่ายเงินแล้วรวม' if claim.status == ClaimRequest.Status.PAID else 'รับเงินแล้ว'
        amount_context += f'{funding_label}: {funded_total:,.2f} บาท\n'
        if supplemental_paid:
            amount_context += f'จ่ายเพิ่มแล้ว: {supplemental_paid:,.2f} บาท\n'
        if still_due:
            due_label = 'ส่วนต่างที่ไม่พบประวัติจ่าย' if completed else 'ต้องจ่ายเพิ่ม'
            amount_context += f'{due_label}: {still_due:,.2f} บาท\n'
        return_label = 'คืนเงินแล้ว' if completed else 'ต้องคืนเงิน'
        amount_context += f'{return_label}: {returned:,.2f} บาท\n'
    elif completed:
        amount_context += 'ยอดใช้จริงรวม: ยังไม่ได้บันทึก\n'
    if claim.expense_month:
        amount_context += f'ค่าใช้จ่ายของเดือน: {claim.expense_month_display}\n'
    if claim.status in (ClaimRequest.Status.PENDING_SUPERVISOR, ClaimRequest.Status.PENDING_EXECUTIVE,
                        ClaimRequest.Status.PENDING_ACCOUNTING, ClaimRequest.Status.PENDING_EXCESS_APPROVAL) and not cfo_ack_stage:
        daily_total = claim.excess_daily_total if claim.status == ClaimRequest.Status.PENDING_EXCESS_APPROVAL else claim.daily_requested_total
        if daily_total is not None:
            amount_context += f'ยอดสะสมใช้อนุมัติ รวมคำขอนี้: {daily_total:,.2f} บาท\n'
            if daily_total > claim.cfo_threshold:
                amount_context += f'ยอดสะสมเกินเกณฑ์ {claim.cfo_threshold:,.2f} บาท ต้องให้ CFO อนุมัติ\n'
                amount_context += daily_approval_breakdown(claim, daily_total,
                    include_actual=claim.status == ClaimRequest.Status.PENDING_EXCESS_APPROVAL)
    if not cfo_ack_stage and claim.status == ClaimRequest.Status.PENDING_PAYMENT:
        daily_total = claim.excess_daily_total if claim.has_excess else claim.daily_requested_total
        if daily_total is not None:
            amount_context += f'ยอดสะสมสำหรับอนุมัติ รวมคำขอนี้: {daily_total:,.2f} บาท\n'
            if daily_total > claim.cfo_threshold:
                amount_context += daily_approval_breakdown(claim, daily_total, include_actual=claim.has_excess)
    elif not cfo_ack_stage and completed:
        daily_actual = claim.calculate_completed_daily_actual_total()
        amount_context += (
            f'ยอดใช้จริงสะสมของวันที่ขอเบิก ({claim.request_date:%d/%m/%Y}): {daily_actual:,.2f} บาท\n'
            'เฉพาะคำขอของผู้ขอคนนี้ที่ปิดรายการแล้วและมียอดใช้จริง\n'
        )
    if not settlement_stage and not budget_departments and claim.has_excess:
        amount_context += f'ยอดใช้จริง: {claim.actual_total:,.2f} บาท\nส่วนต่างที่ขอเพิ่ม: {claim.settlement_amount:,.2f} บาท\n'
    telegram_message = (
        f"{message}\n\n"
        f"{approval_summary}"
        f"เลขที่: {document_number}\n"
        f"ผู้ขอ: {claim.requester.get_full_name() or claim.requester.username} (แผนก {claim.requester.department or claim.department or '-'})\n"
        f"สถานที่: {claim.location_display}\n"
        f"ประเภท: {claim.get_request_type_display()}\n"
        f"วัตถุประสงค์: {claim.purpose}\n"
        f"{amount_label}: {requested_amount:,.2f} บาท\n"
        f"{amount_context}"
        f"{'รายการใช้จริง' if settlement_stage else 'รายการ'}:\n{item_summary}\n"
        f"สถานะ: {claim.workflow_status_display}"
    )
    reply_markup = telegram_acknowledgement_keyboard(claim, cfo_ack_stage) if cfo_ack_stage else (telegram_action_keyboard(claim, actor=user) if claim.can_approve(user) or (claim.status == ClaimRequest.Status.PENDING_PAYMENT and claim.accountant_id == user.id) or (claim.status == ClaimRequest.Status.PAID and claim.requester_id == user.id) else None)
    parts = telegram_message_parts(telegram_message)
    for index, part in enumerate(parts, 1):
        part_title = title if len(parts) == 1 else f'{title} ({index}/{len(parts)})'
        send_telegram_notification(user, part_title, part, link, reply_markup if index == len(parts) else None, status_icon)
        send_telegram_group_notification(part_title, part, link, status_icon)


def receipt_confirmation_message(claim):
    kind = 'ส่วนต่าง' if claim.latest_payment_is_supplemental else ''
    message = f"คำขอ {claim.document_number or ''} บัญชีจ่ายเงิน{kind} {claim.receipt_amount:,.2f} บาทแล้ว กรุณายืนยันการรับเงิน{kind}"
    configuration = get_system_settings()
    if configuration.auto_receive_enabled:
        days = claim.receipt_wait_days(configuration)
        next_step = "ปิดรายการ" if claim.receipt_closes_claim else "ส่งไปรอเคลียร์"
        message += (
            f" ภายใน {days} วันหลังจ่ายเงิน; "
            f"หากไม่กดยืนยัน ระบบจะถือว่ารับเงินแล้วและ{next_step}อัตโนมัติ "
            "หากยังไม่ได้รับเงินจริง กรุณาติดต่อเจ้าหน้าที่บัญชี"
        )
    return message


TELEGRAM_REJECTION_REASONS = {
    "reject-incomplete": "ข้อมูลหรือเอกสารประกอบไม่ครบถ้วน",
    "reject-budget": "รายการไม่เป็นไปตามงบประมาณที่กำหนด",
    "reject-contact-accounting": "กรุณาติดต่อฝ่ายบัญชีเพื่อขอข้อมูลเพิ่มเติม",
}
TELEGRAM_RETURN_REASONS = {
    action.replace("reject-", "return-", 1): reason
    for action, reason in TELEGRAM_REJECTION_REASONS.items()
}


def telegram_acknowledgement_keyboard(claim, stage):
    return {"inline_keyboard": [[{"text": "รับทราบ", "callback_data": f"pc:{claim.id}:ack-{stage}"}]]}


def telegram_approval_context(claim):
    boundary = claim.approval_records.filter(action__in=(
        ClaimApproval.Action.SUBMITTED, ClaimApproval.Action.RETURNED,
        ClaimApproval.Action.EXCESS_SUBMITTED, ClaimApproval.Action.AMOUNT_CORRECTED,
        ClaimApproval.Action.ACTUAL_CORRECTED,
    )).order_by('-pk').values_list('pk', flat=True).first() or 0
    return f'{claim.status}:{bool(claim.accounting_approved_at)}:{bool(claim.excess_accounting_approved_at)}:{claim.amount_revision}:{boundary}'


def telegram_approval_callback_data(claim, action, actor=None):
    actor = actor or claim.current_approver()
    if actor is None and claim.status == ClaimRequest.Status.PENDING_BUDGET_APPROVAL:
        approval = claim.budget_approvals.filter(approved_at__isnull=True).select_related('approver').order_by('pk').first()
        actor = approval.approver if approval else None
    if actor is None:
        raise ValueError('ไม่พบผู้อนุมัติสำหรับปุ่ม Telegram')
    prompt, _ = TelegramApprovalPrompt.objects.get_or_create(
        claim_request=claim, actor=actor, context=telegram_approval_context(claim),
    )
    data = f'pa:{prompt.token.hex}:{action}'
    if len(data.encode('utf-8')) > 64:
        raise ValueError('ปุ่ม Telegram มีข้อมูลยาวเกินกำหนด')
    return data


def telegram_action_keyboard(claim, actor=None):
    """Return only the actions valid for the claim's current workflow step."""
    if claim.status in (ClaimRequest.Status.PENDING_BUDGET_APPROVAL, ClaimRequest.Status.PENDING_SUPERVISOR,
                        ClaimRequest.Status.PENDING_EXECUTIVE, ClaimRequest.Status.PENDING_ACCOUNTING,
                        ClaimRequest.Status.PENDING_EXCESS_APPROVAL):
        return {
            "inline_keyboard": [
                [
                    {"text": "✅ กดอนุมัติ", "callback_data": telegram_approval_callback_data(claim, 'approve', actor)},
                    {"text": "❌ กดไม่อนุมัติ", "callback_data": telegram_approval_callback_data(claim, 'reject-menu', actor)},
                ],
                [{"text": "↩️ ส่งกลับแก้ไข", "callback_data": telegram_approval_callback_data(claim, 'return-menu', actor)}],
            ]
        }
    if claim.status == ClaimRequest.Status.PENDING_PAYMENT:
        if public_base_url().startswith("https://"):
            detail_url = f"{public_base_url()}{reverse('claim_detail', kwargs={'claim_id': claim.id})}"
            return {"inline_keyboard": [[{"text": "💸 เปิดหน้าบันทึกจ่ายเงิน", "url": detail_url}]]}
        return None
    if claim.status == ClaimRequest.Status.PAID:
        event = claim.latest_payment_event
        action = f'receive-{event.pk}' if event else 'receive'
        return {
            "inline_keyboard": [
                [{"text": "✅ กดยืนยันรับเงินส่วนต่าง" if claim.latest_payment_is_supplemental else "✅ กดยืนยันรับเงิน", "callback_data": f"pc:{claim.id}:{action}"}],
            ]
        }
    return None


def telegram_action_recipient(claim):
    """Return the user allowed to act from Telegram for this status."""
    if claim.status == ClaimRequest.Status.PAID:
        return claim.requester
    return claim.current_approver()


def telegram_rejection_keyboard(claim, *, returning=False, actor=None):
    action_prefix = "return" if returning else "reject"
    return {
        "inline_keyboard": [
            [{"text": "ข้อมูลหรือเอกสารไม่ครบ", "callback_data": telegram_approval_callback_data(claim, f'{action_prefix}-incomplete', actor)}],
            [{"text": "ไม่เป็นไปตามงบประมาณ", "callback_data": telegram_approval_callback_data(claim, f'{action_prefix}-budget', actor)}],
            [{"text": "กรุณาติดต่อฝ่ายบัญชี", "callback_data": telegram_approval_callback_data(claim, f'{action_prefix}-contact-accounting', actor)}],
            [{"text": "⬅️ ย้อนกลับไปเลือกอนุมัติ / ไม่อนุมัติ", "callback_data": telegram_approval_callback_data(claim, 'approval-menu', actor)}],
        ]
    }


@transaction.atomic
def negative_approval_step(claim, actor, *, returning, comment):
    """Apply a return/rejection once against the latest actor and workflow state."""
    locked = ClaimRequest.objects.select_for_update().get(pk=claim.pk)
    if locked.status not in (ClaimRequest.Status.PENDING_BUDGET_APPROVAL, ClaimRequest.Status.PENDING_SUPERVISOR,
            ClaimRequest.Status.PENDING_EXECUTIVE, ClaimRequest.Status.PENDING_ACCOUNTING, ClaimRequest.Status.PENDING_EXCESS_APPROVAL):
        return None
    if not is_system_admin(actor) and not locked.can_approve(actor):
        return None
    before = locked.status
    fields = ['status', 'updated_at']
    if returning and before == ClaimRequest.Status.PENDING_EXCESS_APPROVAL:
        # Initial funding is already paid; return actual-use clearance, not the original claim.
        locked.status = ClaimRequest.Status.PENDING_CLEARANCE
        for field in ('excess_accounting_approved_at', 'excess_cfo_approved_at',
                      'cfo_excess_acknowledged_at', 'excess_daily_total', 'excess_exposure_details', 'excess_cfo_required'):
            setattr(locked, field, None)
            fields.append(field)
    else:
        locked.status = ClaimRequest.Status.RETURNED if returning or before == ClaimRequest.Status.PENDING_BUDGET_APPROVAL else ClaimRequest.Status.REJECTED
    locked.save(update_fields=fields)
    record_action = ClaimApproval.Action.RETURNED if returning or before == ClaimRequest.Status.PENDING_BUDGET_APPROVAL else ClaimApproval.Action.REJECTED
    add_approval_record(locked, actor, record_action, before, comment)
    log_event(actor, 'RETURN_CLAIM' if returning else 'REJECT_CLAIM', locked.document_number or f'คำขอ #{locked.pk}', comment)
    for field in fields:
        setattr(claim, field, getattr(locked, field))
    return before


def process_telegram_approval(claim, actor, action, payment_event_id=None):
    """Apply the same approval state changes as the web approval screen."""
    if action == "pay":
        return "กรุณาเปิดหน้าเว็บเพื่อเลือกวิธีจ่ายเงินและแนบสลิปเมื่อโอน"
    if action == "receive":
        claim = confirm_receipt_step(claim, actor, payment_event_id)
        if claim is None:
            return "รายการนี้ดำเนินการไปแล้ว"
        next_step = "ปิดรายการแล้ว" if claim.status == ClaimRequest.Status.COMPLETED else "อยู่ระหว่างรอเคลียร์รายการ"
        kind = 'ส่วนต่าง' if claim.latest_payment_is_supplemental else ''
        notify(claim.requester, f"ผู้ขอยืนยันรับเงิน{kind}แล้ว", f"คำขอ {claim.document_number or ''} ยืนยันรับเงิน{kind} {claim.receipt_amount:,.2f} บาทแล้ว {next_step}", claim)
        return "ยืนยันรับเงินเรียบร้อยแล้ว"
    before = claim.status
    if action == "approve":
        if not approve_claim_step(claim, actor):
            return "รายการนี้ดำเนินการไปแล้ว"
        record_action = ClaimApproval.Action.APPROVED
        comment = ""
        outcome = "อนุมัติคำขอเรียบร้อยแล้ว"
    elif action in TELEGRAM_RETURN_REASONS:
        comment = TELEGRAM_RETURN_REASONS[action]
        before = negative_approval_step(claim, actor, returning=True, comment=comment)
        if before is None:
            return "รายการนี้ดำเนินการไปแล้ว"
        outcome = f"ส่งคำขอกลับให้ผู้ขอแก้ไขแล้ว: {comment}"
    else:
        comment = TELEGRAM_REJECTION_REASONS[action]
        before = negative_approval_step(claim, actor, returning=False, comment=comment)
        if before is None:
            return "รายการนี้ดำเนินการไปแล้ว"
        outcome = f"ไม่อนุมัติ Budget และส่งกลับผู้ขอแล้ว: {comment}" if before == ClaimRequest.Status.PENDING_BUDGET_APPROVAL else f"ไม่อนุมัติคำขอแล้ว: {comment}"

    if action == "approve":
        claim.save(update_fields=["status", "updated_at"])
        add_approval_record(claim, actor, record_action, before, comment)
        log_event(actor, "APPROVE_CLAIM", claim.document_number or f"คำขอ #{claim.id}", comment)
        notify_next_approvers(claim, before)
    elif action in TELEGRAM_RETURN_REASONS:
        notify(claim.requester, "ยอดใช้จริงถูกส่งกลับแก้ไข" if claim.status == ClaimRequest.Status.PENDING_CLEARANCE else "คำขอถูกส่งกลับแก้ไข", comment, claim)
    else:
        notify(claim.requester, "Budget ไม่ได้รับอนุมัติ กรุณาแก้ไขรายการ" if before == ClaimRequest.Status.PENDING_BUDGET_APPROVAL else "คำขอไม่ได้รับอนุมัติ", comment, claim)
    return outcome


def confirm_telegram_webhook_check(callback: dict) -> None:
    """Confirm an admin's real Telegram callback without touching claim workflow."""
    callback_id = callback.get("id", "")
    token = str(callback.get("data", "")).removeprefix("health:")
    check = TelegramWebhookCheck.objects.select_related("requested_by").filter(token=token).first()
    if not check:
        answer_telegram_callback(callback_id, "ไม่พบการตรวจสอบ Webhook นี้")
        return
    telegram_id = str(callback.get("from", {}).get("id", ""))
    if telegram_id != str(check.requested_by.telegram_user_id or ""):
        answer_telegram_callback(callback_id, "ปุ่มนี้สำหรับผู้ดูแลที่เริ่มการตรวจสอบเท่านั้น")
        return
    if check.confirmed_at:
        answer_telegram_callback(callback_id, "Webhook นี้ยืนยันแล้ว ✅")
        return
    check.confirmed_at = timezone.now()
    check.save(update_fields=["confirmed_at"])
    remove_telegram_callback_keyboard(callback)
    answer_telegram_callback(callback_id, "Webhook ใช้งานได้จริง ✅")
    send_telegram_message(
        check.requested_by.telegram_user_id,
        build_telegram_text("Webhook ใช้งานได้จริง", "ระบบได้รับ callback จาก Telegram และยืนยันการเชื่อมต่อเรียบร้อยแล้ว"),
    )
    log_event(check.requested_by, "TEST_TELEGRAM_WEBHOOK", "Telegram Webhook", "ผู้ดูแลกดยืนยันจาก Telegram สำเร็จ")


@csrf_exempt
@transaction.atomic
def telegram_webhook(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    from secrets import compare_digest
    secret = telegram_secret()
    if not secret or not compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret):
        return HttpResponse(status=403)
    try:
        callback = json.loads(request.body).get("callback_query")
    except (TypeError, ValueError, json.JSONDecodeError):
        return HttpResponse(status=400)
    if not callback:
        return HttpResponse(status=200)

    if str(callback.get("data", "")).startswith("health:"):
        confirm_telegram_webhook_check(callback)
        return HttpResponse(status=200)

    callback_id = callback.get("id", "")
    if not isinstance(callback_id, str) or not callback_id or len(callback_id) > 200:
        return HttpResponse(status=400)
    _, first_delivery = TelegramCallbackReceipt.objects.get_or_create(callback_id=callback_id)
    if not first_delivery:
        answer_telegram_callback(callback_id, "รายการนี้ดำเนินการไปแล้ว")
        return HttpResponse(status=200)
    data = callback.get("data", "")
    payment_event_id = None
    prompt = None
    approval_actions = {"approve", "reject-menu", "return-menu", "approval-menu", *TELEGRAM_REJECTION_REASONS, *TELEGRAM_RETURN_REASONS}
    try:
        prefix, claim_id, action = data.split(":", 2)
        if prefix == 'pa':
            prompt = TelegramApprovalPrompt.objects.select_related('actor').get(token=UUID(claim_id))
            claim_id = prompt.claim_request_id
            if action not in approval_actions:
                raise ValueError
        elif prefix == 'pc' and action in approval_actions:
            answer_telegram_callback(callback_id, "ปุ่มอนุมัติรุ่นเก่าหมดอายุ กรุณาเปิดหน้าเว็บเพื่อทำรายการ")
            remove_telegram_callback_keyboard(callback)
            return HttpResponse(status=200)
        if action.startswith('receive-') and action.removeprefix('receive-').isdigit():
            payment_event_id = action.removeprefix('receive-')
            action = 'receive'
        if prefix not in ('pc', 'pa') or action not in {"approve", "pay", "receive", "reject-menu", "return-menu", "approval-menu", "ack-initial", "ack-excess", *TELEGRAM_REJECTION_REASONS, *TELEGRAM_RETURN_REASONS}:
            raise ValueError
        claim = ClaimRequest.objects.select_for_update(of=('self',)).select_related("requester", "supervisor", "executive", "accounting_manager", "accountant", "cfo").get(pk=int(claim_id))
    except (ValueError, ClaimRequest.DoesNotExist, TelegramApprovalPrompt.DoesNotExist):
        answer_telegram_callback(callback_id, "ไม่พบคำขอหรือปุ่มนี้หมดอายุแล้ว")
        return HttpResponse(status=200)

    if prompt is not None:
        if (not prompt.actor.is_active or
                str(prompt.actor.telegram_user_id or '') != str(callback.get('from', {}).get('id', ''))):
            answer_telegram_callback(callback_id, 'ปุ่มนี้สำหรับผู้อนุมัติที่ได้รับแจ้งเท่านั้น')
            return HttpResponse(status=200)
        if prompt.context != telegram_approval_context(claim) or not claim.can_approve(prompt.actor):
            remove_telegram_callback_keyboard(callback)
            answer_telegram_callback(callback_id, 'ปุ่มนี้หมดอายุแล้ว กรุณาใช้ข้อความอนุมัติขั้นล่าสุด')
            return HttpResponse(status=200)

    if action in {"ack-initial", "ack-excess"}:
        stage = action.removeprefix("ack-")
        telegram_id = str(callback.get("from", {}).get("id", ""))
        actor = User.objects.filter(pk=claim.cfo_id, telegram_user_id=telegram_id, is_active=True).first()
        if not actor:
            answer_telegram_callback(callback_id, "ปุ่มนี้สำหรับ CFO ที่ได้รับแจ้งเท่านั้น")
        elif acknowledge_cfo(claim, actor, stage):
            remove_telegram_callback_keyboard(callback)
            answer_telegram_callback(callback_id, "บันทึกรับทราบแล้ว")
        else:
            remove_telegram_callback_keyboard(callback)
            answer_telegram_callback(callback_id, "รายการนี้รับทราบแล้วหรือปุ่มหมดอายุ")
        return HttpResponse(status=200)

    # A button may remain in a previously delivered Telegram message after
    # another channel (for example, the web screen) has completed the action.
    # Report that state before resolving the current approver, which may have
    # changed as part of the web workflow.
    if claim.status not in (
        ClaimRequest.Status.PENDING_BUDGET_APPROVAL,
        ClaimRequest.Status.PENDING_SUPERVISOR,
        ClaimRequest.Status.PENDING_EXECUTIVE,
        ClaimRequest.Status.PENDING_ACCOUNTING,
        ClaimRequest.Status.PENDING_EXCESS_APPROVAL,
        ClaimRequest.Status.PENDING_PAYMENT,
        ClaimRequest.Status.PAID,
    ):
        remove_telegram_callback_keyboard(callback)
        answer_telegram_callback(callback_id, "รายการนี้ดำเนินการไปแล้ว")
        return HttpResponse(status=200)
    if claim.status == ClaimRequest.Status.PENDING_PAYMENT and action == "pay":
        remove_telegram_callback_keyboard(callback)
        answer_telegram_callback(callback_id, "กรุณาเปิดหน้าเว็บเพื่อเลือกวิธีจ่ายเงินและแนบสลิปเมื่อโอน")
        return HttpResponse(status=200)
    allowed_actions = {
        ClaimRequest.Status.PENDING_BUDGET_APPROVAL: approval_actions,
        ClaimRequest.Status.PENDING_SUPERVISOR: approval_actions,
        ClaimRequest.Status.PENDING_EXECUTIVE: approval_actions,
        ClaimRequest.Status.PENDING_ACCOUNTING: approval_actions,
        ClaimRequest.Status.PENDING_EXCESS_APPROVAL: approval_actions,
        ClaimRequest.Status.PENDING_PAYMENT: set(),
        ClaimRequest.Status.PAID: {"receive"},
    }
    if action not in allowed_actions[claim.status]:
        remove_telegram_callback_keyboard(callback)
        answer_telegram_callback(callback_id, "รายการนี้ดำเนินการไปแล้ว")
        return HttpResponse(status=200)

    telegram_id = str(callback.get("from", {}).get("id", ""))
    candidates = list(User.objects.filter(telegram_user_id=telegram_id, is_active=True).prefetch_related("groups"))
    expected_actor = telegram_action_recipient(claim)
    actor = prompt.actor if prompt else next((user for user in candidates if claim.can_approve(user) or (expected_actor and user.id == expected_actor.id)), None)
    if not actor:
        actor = next((user for user in candidates if is_system_admin(user)), None)
    if not actor:
        answer_telegram_callback(callback_id, "ยังไม่ถึงขั้นอนุมัติของคุณ" if candidates else "ยังไม่ได้ผูก Telegram กับผู้ใช้ในระบบ")
        return HttpResponse(status=200)
    if not is_system_admin(actor) and not claim.can_approve(actor) and telegram_action_recipient(claim) != actor:
        answer_telegram_callback(callback_id, "คุณไม่มีสิทธิ์ดำเนินการรายการนี้")
        return HttpResponse(status=200)

    if action in {"reject-menu", "return-menu", "approval-menu"}:
        returning = action == "return-menu"
        going_back = action == "approval-menu"
        keyboard = telegram_action_keyboard(claim, actor=actor) if going_back else telegram_rejection_keyboard(claim, returning=returning, actor=actor)
        title = "กลับมาเลือกการอนุมัติ" if going_back else ("เลือกเหตุผลที่ส่งกลับแก้ไข" if returning else "เลือกเหตุผลที่ไม่อนุมัติ")
        if not replace_telegram_callback_keyboard(callback, keyboard):
            remove_telegram_callback_keyboard(callback)
            send_telegram_notification(actor, title,
                f"คำขอ {claim.document_number or f'#{claim.pk}'} · {claim.workflow_status_display}\nกรุณาเลือกจากปุ่มด้านล่าง",
                link=reverse('claim_detail', kwargs={'claim_id': claim.pk}), reply_markup=keyboard)
        answer_telegram_callback(callback_id, title)
        return HttpResponse(status=200)

    outcome = process_telegram_approval(claim, actor, action, payment_event_id)
    remove_telegram_callback_keyboard(callback)
    answer_telegram_callback(callback_id, outcome)
    return HttpResponse(status=200)


def filtered_claims(request):
    """Return the claim list permitted to the current user with GET filters applied."""
    claims = ClaimRequest.objects.select_related("requester", "supervisor", "executive", "accounting_manager", "accountant", "cfo")
    selected_scope = request.GET.get("scope", "") if not is_system_admin(request.user) else ""
    if selected_scope not in {"completed", "cancelled"}:
        selected_scope = ""
    only_mine = request.GET.get("mine") == "1" and not is_system_admin(request.user) and not selected_scope
    if selected_scope == "completed":
        claims = claims.filter(status=ClaimRequest.Status.COMPLETED).filter(
            Q(requester=request.user) | Q(accountant=request.user)
        ).distinct()
    elif selected_scope == "cancelled":
        claims = claims.filter(status=ClaimRequest.Status.CANCELLED).filter(
            Q(requester=request.user)
            | Q(approval_records__actor=request.user, approval_records__action=ClaimApproval.Action.CANCELLED)
        ).distinct()
    elif only_mine:
        claims = claims.filter(requester=request.user)
    elif not is_system_admin(request.user):
        selected_scope = ""
        claims = claims.filter(
            Q(requester=request.user)
            | Q(budget_approvals__approver=request.user, budget_approvals__approved_at__isnull=True, status=ClaimRequest.Status.PENDING_BUDGET_APPROVAL)
            | Q(supervisor=request.user, status=ClaimRequest.Status.PENDING_SUPERVISOR)
            | Q(executive=request.user, status=ClaimRequest.Status.PENDING_EXECUTIVE)
            | Q(accounting_manager=request.user, status__in=(ClaimRequest.Status.PENDING_ACCOUNTING, ClaimRequest.Status.PENDING_EXCESS_APPROVAL))
            | Q(cfo=request.user)
            | Q(accountant=request.user, status__in=(ClaimRequest.Status.PENDING_PAYMENT, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW, ClaimRequest.Status.COMPLETED))
            | Q(approval_records__actor=request.user)
        ).distinct()
    queue_labels = {
        "accounting_approval": "รอบัญชีอนุมัติ",
        "cfo_approval": "รอ CFO อนุมัติ",
        "cfo_acknowledgement": "รอ CFO รับทราบ",
    }
    selected_queue = request.GET.get("queue", "")
    if selected_queue == "accounting_approval":
        claims = claims.filter(
            status=ClaimRequest.Status.PENDING_ACCOUNTING,
            accounting_approved_at__isnull=True,
        )
    elif selected_queue == "cfo_approval":
        claims = claims.filter(pending_cfo_approval_q())
    elif selected_queue == "cfo_acknowledgement":
        claims = claims.filter(pk__in=pending_cfo_acknowledgement_ids(claims))
    else:
        selected_queue = ""
    selected_status = request.GET.get("status", "")
    selected_status_group = request.GET.get("status_group", "")
    if only_mine and selected_status_group in REQUESTER_STATUS_GROUPS:
        claims = claims.filter(status__in=REQUESTER_STATUS_GROUPS[selected_status_group])
    else:
        selected_status_group = ""
    status_labels = dict(ClaimRequest.Status.choices)
    if selected_status in status_labels:
        claims = claims.filter(status=selected_status)
    else:
        selected_status = ""
    search_query = request.GET.get("q", "").strip()
    work_location = request.GET.get("work_location", "")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    if search_query:
        claims = claims.filter(
            Q(document_number__icontains=search_query)
            | Q(purpose__icontains=search_query)
            | Q(requester__username__icontains=search_query)
            | Q(requester__first_name__icontains=search_query)
            | Q(requester__last_name__icontains=search_query)
        )
    if work_location in dict(ClaimRequest._meta.get_field("work_location").choices):
        claims = claims.filter(work_location=work_location)
    else:
        work_location = ""
    if date_from:
        claims = claims.filter(request_date__gte=date_from)
    if date_to:
        claims = claims.filter(request_date__lte=date_to)
    return claims, {
        "only_mine": only_mine,
        "selected_scope": selected_scope,
        "selected_queue": selected_queue,
        "selected_queue_label": queue_labels.get(selected_queue, ""),
        "selected_status": selected_status,
        "selected_status_group": selected_status_group,
        "selected_status_group_label": REQUESTER_STATUS_GROUP_LABELS.get(selected_status_group, ""),
        "selected_status_label": status_labels.get(selected_status, ""),
        "search_query": search_query,
        "work_location": work_location,
        "date_from": date_from,
        "date_to": date_to,
    }


@login_required
def claim_list(request):
    claims, filters = filtered_claims(request)
    claims, page_obj, page_size, query_params = paginate(request, claims.order_by("-updated_at"))
    return render(request, "claims/claim_list.html", {
        "claims": claims, "page_obj": page_obj, "page_size": page_size, "query_params": query_params,
        "users": [], "audit_logs": [],
        "is_system_admin": is_system_admin(request.user),
        "can_export_claims": can_export_claims(request.user),
        **filters,
        "status_choices": [
            (code, label) for code, label in ClaimRequest.Status.choices
            if not filters["selected_status_group"] or code in REQUESTER_STATUS_GROUPS[filters["selected_status_group"]]
        ],
        "work_location_choices": ClaimRequest._meta.get_field("work_location").choices,
    })


@login_required
def claim_export(request):
    if not can_export_claims(request.user):
        return HttpResponseForbidden("คุณไม่มีสิทธิ์ส่งออกข้อมูล")

    claims, filters = filtered_claims(request)
    claims = list(claims.order_by("request_date", "document_number", "pk").select_related(
        'requester', 'accountant', 'supervisor', 'accounting_manager', 'cfo', 'executive',
    ).prefetch_related('items__budget_approver', 'payments__paid_by'))
    workbook = build_claim_report(claims, filters)
    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(output.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="petty-cash-claims-{timezone.localdate():%Y%m%d}.xlsx"'
    log_event(request.user, "EXPORT_CLAIMS", "รายการคำขอเบิก")
    return response


@login_required
def claim_create(request):
    claim = ClaimRequest()
    form = ClaimRequestForm(request.POST or None, instance=claim, requester=request.user)
    formset = ClaimItemFormSet(request.POST or None, instance=claim, prefix="items", form_kwargs={"requester": request.user})
    uploads = request.FILES.getlist("attachments")
    upload_errors = attachment_errors(uploads)
    if request.method == "POST" and request.POST.get("request_type") == ClaimRequest.RequestType.PETTY_CASH and not uploads:
        upload_errors.append("เบิกเงินสดย่อย (ออกเงินไปก่อน) ต้องแนบเอกสารประกอบก่อนบันทึกคำขอ")
    if request.method == "POST" and form.is_valid() and formset.is_valid() and form.validate_approvers(formset) and not upload_errors:
        with transaction.atomic():
            claim = form.save()
            formset.instance = claim
            items = formset.save(commit=False)
            for removed_item in formset.deleted_objects:
                removed_item.delete()
            for index, item in enumerate(items, start=1):
                item.sort_order = index
                item.save()
            first_item = claim.items.first()
            if first_item:
                claim.business_unit, claim.department, claim.budget = first_item.business_unit, first_item.department, first_item.budget
                claim.save(update_fields=["business_unit", "department", "budget", "updated_at"])
            for upload in uploads:
                ClaimAttachment.objects.create(claim_request=claim, file=upload, original_name=upload.name, uploaded_by=request.user)
            claim.refresh_requested_total()
        log_event(request.user, "CREATE_CLAIM", f"คำขอ #{claim.id}", claim.purpose)
        messages.success(request, "บันทึกแบบร่างคำขอเบิกแล้ว")
        return redirect("claim_detail", claim_id=claim.id)
    return render(request, "claims/claim_form.html", {"form": form, "formset": formset, "upload_errors": upload_errors, "page_title": "สร้างคำขอเบิก", "executive_approval_threshold": get_system_settings().executive_approval_threshold, **claim_item_choices(request.user)})


@login_required
def claim_edit(request, claim_id):
    claim = owned_claim_or_404(request.user, claim_id)
    if claim.status not in (ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED):
        messages.error(request, "แก้ไขได้เฉพาะแบบร่างหรือคำขอที่ส่งกลับแก้ไขเท่านั้น")
        return redirect("claim_detail", claim_id=claim.id)
    form = ClaimRequestForm(request.POST or None, instance=claim, requester=request.user)
    formset = ClaimItemFormSet(request.POST or None, instance=claim, prefix="items", form_kwargs={"requester": request.user})
    uploads = request.FILES.getlist("attachments")
    upload_errors = attachment_errors(uploads, claim.attachments.count())
    if request.method == "POST" and request.POST.get("request_type") == ClaimRequest.RequestType.PETTY_CASH and not uploads and not claim.attachments.exists():
        upload_errors.append("เบิกเงินสดย่อย (ออกเงินไปก่อน) ต้องแนบเอกสารประกอบก่อนบันทึกคำขอ")
    if request.method == "POST" and form.is_valid() and formset.is_valid() and form.validate_approvers(formset) and not upload_errors:
        with transaction.atomic():
            claim = form.save()
            items = formset.save(commit=False)
            for removed_item in formset.deleted_objects:
                removed_item.delete()
            for index, item in enumerate(items, start=1):
                item.sort_order = index
                item.save()
            first_item = claim.items.order_by("sort_order", "pk").first()
            if first_item:
                claim.business_unit, claim.department, claim.budget = first_item.business_unit, first_item.department, first_item.budget
                claim.save(update_fields=["business_unit", "department", "budget", "updated_at"])
            for upload in uploads:
                ClaimAttachment.objects.create(claim_request=claim, file=upload, original_name=upload.name, uploaded_by=request.user)
            claim.refresh_requested_total()
        log_event(request.user, "UPDATE_CLAIM", claim.document_number or f"คำขอ #{claim.id}", claim.purpose)
        messages.success(request, "บันทึกการแก้ไขแล้ว")
        return redirect("claim_detail", claim_id=claim.id)
    page_title = "แก้ไขแบบร่าง" if claim.status == ClaimRequest.Status.DRAFT else "แก้ไขคำขอที่ส่งกลับ"
    return render(request, "claims/claim_form.html", {"form": form, "formset": formset, "upload_errors": upload_errors, "page_title": page_title, "claim": claim, "executive_approval_threshold": get_system_settings().executive_approval_threshold, **claim_item_choices(request.user)})


@login_required
def claim_detail(request, claim_id):
    claim = visible_claim_or_404(request.user, claim_id)
    claim.approval_references = reference_rows(claim) if can_view_references(request.user, claim) else []
    claim.exposure_total = claim.excess_daily_total if claim.excess_exposure_details is not None else claim.daily_requested_total
    is_admin = is_system_admin(request.user)
    claim.completed_daily_actual_total = None
    if is_admin or request.user.id in {claim.requester_id, claim.accountant_id, claim.accounting_manager_id, claim.cfo_id}:
        claim.completed_daily_actual_total = claim.calculate_completed_daily_actual_total()
    configuration = get_system_settings()
    max_attachment_count = configuration.max_attachment_count
    can_approve = (is_admin or claim.can_approve(request.user)) and claim.status in (
        ClaimRequest.Status.PENDING_BUDGET_APPROVAL,
        ClaimRequest.Status.PENDING_SUPERVISOR, ClaimRequest.Status.PENDING_EXECUTIVE,
        ClaimRequest.Status.PENDING_ACCOUNTING, ClaimRequest.Status.PENDING_EXCESS_APPROVAL,
    )
    can_pay = claim.status == ClaimRequest.Status.PENDING_PAYMENT and claim.requester_id != request.user.id and (is_admin or claim.accountant_id == request.user.id)
    can_confirm_receipt = claim.status == ClaimRequest.Status.PAID and (is_admin or claim.requester_id == request.user.id)
    can_submit_clearance = claim.status == ClaimRequest.Status.PENDING_CLEARANCE and (is_admin or claim.requester_id == request.user.id)
    can_complete = claim.status == ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW and (is_admin or claim.accountant_id == request.user.id)
    can_correct_actual = claim.request_type == ClaimRequest.RequestType.ADVANCE and (can_complete or (claim.status == ClaimRequest.Status.PENDING_PAYMENT and claim.has_excess and (is_admin or claim.accountant_id == request.user.id)))
    can_correct_requested = can_pay and claim.can_correct_before_payment
    can_cancel_after_refund = claim.status == ClaimRequest.Status.PENDING_CLEARANCE and claim.accountant_id == request.user.id
    can_print = configuration.claim_print_enabled and claim.status == ClaimRequest.Status.COMPLETED and (is_admin or claim.accountant_id == request.user.id)
    can_view_actual = claim.actual_total is not None and (is_admin or request.user.id in {claim.requester_id, claim.accountant_id, claim.accounting_manager_id, claim.cfo_id})
    can_view_payments = can_view_claim_payments(request.user, claim)
    receipt_after_days = claim.receipt_wait_days(configuration)
    cfo_ack_stage = next((stage for stage in ("excess", "initial") if claim.can_cfo_acknowledge(request.user, stage)), None)
    return render(request, "claims/claim_detail.html", {"claim": claim, "can_approve": can_approve, "can_pay": can_pay, "can_correct_requested": can_correct_requested, "can_confirm_receipt": can_confirm_receipt, "can_submit_clearance": can_submit_clearance, "can_complete": False, "can_review_clearance": can_complete, "can_correct_actual": can_correct_actual, "can_cancel_after_refund": can_cancel_after_refund, "can_print": can_print, "can_view_actual": can_view_actual, "can_view_payments": can_view_payments, "settlement_amount": claim.settlement_amount, "has_excess": claim.has_excess, "cfo_ack_stage": cfo_ack_stage, "max_attachment_count": max_attachment_count, "remaining_attachment_slots": max(0, max_attachment_count - claim.attachments.count()), "auto_receive_enabled": configuration.auto_receive_enabled, "receipt_after_days": receipt_after_days})


@login_required
def claim_cfo_acknowledge(request, claim_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claim = visible_claim_or_404(request.user, claim_id)
    stage = request.POST.get("stage", "")
    if acknowledge_cfo(claim, request.user, stage):
        messages.success(request, "บันทึกการรับทราบแล้ว")
    else:
        messages.info(request, "รายการนี้รับทราบแล้วหรือคุณไม่มีสิทธิ์รับทราบ")
    return redirect("claim_detail", claim_id=claim.id)


@login_required
def claim_submit(request, claim_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claim = owned_claim_or_404(request.user, claim_id)
    if claim.status in (ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED):
        error = expense_month_error(claim.expense_month)
        if error or claim.work_location not in ('HQ', 'BRANCH'):
            messages.error(request, error or 'กรุณาแก้ไขสถานที่เป็นสำนักงานใหญ่หรือกาญจนบุรีก่อนส่ง')
            return redirect('claim_detail', claim_id=claim.pk)
    if claim.status in (ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED) and (
        not claim.accountant_id or claim.accountant_id == claim.requester_id
        or not claim.accountant.is_active
        or (claim.requester.alternate_payer_id and claim.requester.groups.filter(name=ACCOUNTANT).exists())
    ):
        claim.accountant = claim.requester.claim_payer()
        claim.save(update_fields=["accountant", "updated_at"])
    if not claim.supervisor_id or not claim.accounting_manager_id or not claim.accountant_id or not claim.cfo_id:
        if not claim.supervisor_id:
            claim.supervisor = claim.requester.supervisor
        claim.accounting_manager = claim.requester.accounting_manager
        claim.accountant = claim.requester.claim_payer()
        claim.cfo = get_system_settings().cfo
        claim.save(update_fields=["supervisor", "accounting_manager", "accountant", "cfo", "updated_at"])
    if claim.status not in (ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED):
        messages.error(request, "ส่งอนุมัติได้เฉพาะแบบร่างหรือรายการที่ถูกส่งกลับ")
    elif not claim.can_submit():
        messages.error(request, "ข้อมูลยังไม่ครบ: ตรวจเดือนค่าใช้จ่าย, สายอนุมัติ, CFO, ผู้จ่ายที่ไม่ใช่ผู้ขอ (ตั้งผู้จ่ายสำรองในข้อมูลผู้ใช้ได้) และไฟล์แนบ")
    else:
        external_approvers = {}
        for item in claim.items.select_related("budget_approver"):
            department = item.department or claim.department
            budget_name = item.budget or claim.budget
            if department == claim.requester.department:
                continue
            manager = item.budget_approver
            if not (manager and manager.is_active and manager.position_level == User.PositionLevel.MANAGER and manager.department == department and Budget.objects.filter(department=department, name=budget_name).exists()):
                messages.error(request, "รายการใช้ Budget ของแผนกอื่นยังไม่ครบ กรุณาแก้ไขรายการและเลือก Manager ของแผนกนั้นก่อนส่ง")
                return redirect("claim_detail", claim_id=claim.id)
            external_approvers[(department, manager.pk)] = manager
        with transaction.atomic():
            User.objects.select_for_update().get(pk=claim.requester_id)
            locked = ClaimRequest.objects.select_for_update().get(pk=claim.pk)
            if locked.status not in (ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED):
                messages.info(request, 'คำขอนี้ส่งอนุมัติไปแล้ว')
                return redirect('claim_detail', claim_id=claim.pk)
            try:
                assign_document_number(claim)
            except ValueError as error:
                messages.error(request, str(error))
                return redirect('claim_detail', claim_id=claim.pk)
            before = claim.status
            claim.requester_position_level = claim.requester.position_level
            claim.corrected_cfo_required = False
            claim.approval_threshold = get_system_settings().executive_approval_threshold
            claim.capture_exposure()
            claim.accepted_request_date = claim.request_date
            claim.excess_exposure_details = None
            claim.excess_daily_total = None
            claim.excess_cfo_required = None
            claim.excess_accounting_approved_at = None
            claim.excess_cfo_approved_at = None
            claim.budget_approvals.all().delete()
            for (department, manager_id), _manager in external_approvers.items():
                ClaimBudgetApproval.objects.create(claim_request=claim, department=department, approver_id=manager_id)
            claim.status = ClaimRequest.Status.PENDING_BUDGET_APPROVAL if external_approvers else ClaimRequest.Status.PENDING_SUPERVISOR
            claim.accounting_approved_at = None
            claim.cfo_approved_at = None
            claim.cfo_acknowledged_at = None
            claim.cfo_excess_acknowledged_at = None
            claim.save(update_fields=["document_number", "status", "requester_position_level", "corrected_cfo_required", "approval_threshold", "daily_requested_total", "approval_exposure_details", "accepted_request_date", "excess_daily_total", "excess_exposure_details", "excess_cfo_required", "excess_accounting_approved_at", "excess_cfo_approved_at", "accounting_approved_at", "cfo_approved_at", "cfo_acknowledged_at", "cfo_excess_acknowledged_at", "updated_at"])
        add_approval_record(claim, request.user, ClaimApproval.Action.SUBMITTED, before)
        log_event(request.user, "SUBMIT_CLAIM", claim.document_number, claim.purpose)
        notify_next_approvers(claim)
        messages.success(request, "ส่งให้ผู้อนุมัติ Budget พิจารณาก่อนผู้อนุมัติของผู้ขอแล้ว" if external_approvers else "ส่งคำขอให้ผู้อนุมัติที่ผูกไว้พิจารณาแล้ว")
    return redirect("claim_detail", claim_id=claim.id)


@login_required
def claim_approval_action(request, claim_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claim = visible_claim_or_404(request.user, claim_id)
    if (not is_system_admin(request.user) and not claim.can_approve(request.user)) or claim.status not in (
        ClaimRequest.Status.PENDING_BUDGET_APPROVAL, ClaimRequest.Status.PENDING_SUPERVISOR, ClaimRequest.Status.PENDING_EXECUTIVE,
        ClaimRequest.Status.PENDING_ACCOUNTING, ClaimRequest.Status.PENDING_EXCESS_APPROVAL,
    ):
        messages.info(request, "รายการนี้ถูกดำเนินการหรือส่งต่อไปขั้นถัดไปแล้ว ระบบได้อัปเดตสถานะล่าสุดให้คุณ")
        return redirect("claim_detail", claim_id=claim.id)
    action = request.POST.get("action")
    comment = request.POST.get("comment", "").strip()
    if action not in {"approve", "return", "reject"}:
        messages.error(request, "ไม่พบคำสั่งที่ต้องการดำเนินการ")
        return redirect("claim_detail", claim_id=claim.id)
    if action in {"return", "reject"} and not comment:
        messages.error(request, "กรุณาระบุเหตุผลก่อนดำเนินการ")
        return redirect("claim_detail", claim_id=claim.id)
    before = claim.status
    if action == "approve":
        if not approve_claim_step(claim, request.user):
            messages.info(request, "รายการนี้ถูกดำเนินการไปแล้ว")
            return redirect("claim_detail", claim_id=claim.id)
        record_action = ClaimApproval.Action.APPROVED
        message = "อนุมัติคำขอเรียบร้อยแล้ว"
    elif action == "return":
        before = negative_approval_step(claim, request.user, returning=True, comment=comment)
        if before is None:
            messages.info(request, "รายการนี้ดำเนินการไปแล้ว")
            return redirect("claim_detail", claim_id=claim.pk)
        message = "ส่งคำขอกลับให้ผู้ขอแก้ไขแล้ว"
    else:
        before = negative_approval_step(claim, request.user, returning=False, comment=comment)
        if before is None:
            messages.info(request, "รายการนี้ดำเนินการไปแล้ว")
            return redirect("claim_detail", claim_id=claim.pk)
        message = "ไม่อนุมัติ Budget และส่งคำขอกลับให้ผู้ขอแก้ไขแล้ว" if before == ClaimRequest.Status.PENDING_BUDGET_APPROVAL else "ไม่อนุมัติคำขอเรียบร้อยแล้ว"
    if action == "approve":
        claim.save(update_fields=["status", "updated_at"])
        add_approval_record(claim, request.user, record_action, before, comment)
        log_event(request.user, "APPROVE_CLAIM", claim.document_number or f"คำขอ #{claim.id}", comment)
        notify_next_approvers(claim, before)
    elif action == "return":
        notify(claim.requester, "ยอดใช้จริงถูกส่งกลับแก้ไข" if claim.status == ClaimRequest.Status.PENDING_CLEARANCE else "คำขอถูกส่งกลับแก้ไข", comment, claim)
    elif action == "reject":
        notify(claim.requester, "Budget ไม่ได้รับอนุมัติ กรุณาแก้ไขรายการ" if before == ClaimRequest.Status.PENDING_BUDGET_APPROVAL else "คำขอไม่ได้รับอนุมัติ", comment, claim)
    messages.success(request, message)
    return redirect("claim_detail", claim_id=claim.id)


@login_required
@transaction.atomic
def claim_payment(request, claim_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claim = visible_claim_or_404(request.user, claim_id)
    claim = ClaimRequest.objects.select_for_update().get(pk=claim.pk)
    if claim.requester_id == request.user.pk:
        messages.error(request, "ไม่สามารถจ่ายเงินให้ตนเองได้ กรุณาให้เจ้าหน้าที่บัญชีอีกคนเป็นผู้จ่าย")
        return redirect("claim_detail", claim_id=claim.id)
    if claim.status != ClaimRequest.Status.PENDING_PAYMENT or (not is_system_admin(request.user) and claim.accountant_id != request.user.id):
        messages.info(request, "รายการนี้ถูกดำเนินการหรือส่งต่อไปขั้นถัดไปแล้ว ระบบได้อัปเดตสถานะล่าสุดให้คุณ")
        return redirect("claim_detail", claim_id=claim.id)
    method = request.POST.get("payment_method", "")
    revision = request.POST.get("amount_revision")
    if (revision is not None or claim.amount_revision) and revision != str(claim.amount_revision):
        messages.error(request, "ยอดคำขอเปลี่ยนแล้ว กรุณาตรวจยอดล่าสุดก่อนจ่ายเงิน")
        return redirect("claim_detail", claim_id=claim.id)
    slip = request.FILES.get("payment_slip")
    errors = payment_input_errors(method, slip)
    if errors:
        for error in errors:
            messages.error(request, error)
        return redirect("claim_detail", claim_id=claim.id)
    before = claim.status
    payment_kind = ClaimPayment.Kind.SUPPLEMENTAL if claim.has_excess else ClaimPayment.Kind.INITIAL
    payment_amount = claim.settlement_amount if claim.has_excess else claim.requested_total
    if claim.has_excess:
        claim.status = ClaimRequest.Status.PAID
        action = ClaimApproval.Action.SUPPLEMENTAL_PAID
        comment = f"จ่ายเงินส่วนต่าง {claim.settlement_amount:,.2f} บาท รอผู้ขอยืนยันรับเงิน"
    else:
        claim.status = ClaimRequest.Status.PAID
        if claim.request_type == ClaimRequest.RequestType.PETTY_CASH:
            claim.actual_total = claim.requested_total
            for item in claim.items.all():
                item.actual_amount = item.requested_amount
                item.save(update_fields=["actual_amount"])
        action = ClaimApproval.Action.PAID
        comment = ""
    claim.save(update_fields=["status", "actual_total", "updated_at"])
    ClaimPayment.objects.create(
        claim_request=claim, kind=payment_kind, method=method, amount=payment_amount,
        slip=slip or "", slip_original_name=slip.name if slip else "", paid_by=request.user,
    )
    comment = f"{comment} · {ClaimPayment.Method(method).label}" if comment else ClaimPayment.Method(method).label
    add_approval_record(claim, request.user, action, before, comment)
    log_event(request.user, "PAY_CLAIM", claim.document_number or f"คำขอ #{claim.id}", comment)
    notify(claim.requester, "จ่ายเงินส่วนต่างแล้ว" if claim.has_excess else "จ่ายเงินแล้ว", receipt_confirmation_message(claim), claim)
    messages.success(request, "บันทึกจ่ายเงินแล้ว รอผู้ขอยืนยันรับเงิน")
    return redirect("claim_detail", claim_id=claim.id)


@login_required
def claim_receipt_confirm(request, claim_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claim = visible_claim_or_404(request.user, claim_id)
    claim = confirm_receipt_step(claim, request.user, request.POST.get('payment_event_id'))
    if claim is None:
        messages.error(request, "รายการหรือรอบจ่ายเงินนี้เปลี่ยนแล้ว กรุณาเปิดรายละเอียดล่าสุดก่อนยืนยันรับเงิน")
        return redirect("claim_detail", claim_id=claim_id)
    if claim.status == ClaimRequest.Status.COMPLETED:
        kind = 'ส่วนต่าง' if claim.latest_payment_is_supplemental else ''
        notify(claim.requester, f"ยืนยันรับเงิน{kind}และปิดรายการแล้ว", f"คำขอ {claim.document_number or ''} ยืนยันรับเงิน{kind} {claim.receipt_amount:,.2f} บาทและเสร็จสิ้นแล้ว", claim)
        messages.success(request, "ยืนยันรับเงินและปิดรายการเรียบร้อยแล้ว")
    else:
        notify(claim.requester, "กรุณาลงยอดใช้จริง", f"คำขอ {claim.document_number or ''} พร้อมให้ลงรายละเอียดการใช้เงินแล้ว", claim)
        messages.success(request, "ยืนยันรับเงินแล้ว กรุณาลงรายละเอียดและยอดใช้จริง")
    return redirect("claim_detail", claim_id=claim.id)


@login_required
@transaction.atomic
def claim_clearance_submit(request, claim_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claim = visible_claim_or_404(request.user, claim_id)
    User.objects.select_for_update().get(pk=claim.requester_id)
    claim = ClaimRequest.objects.select_for_update().get(pk=claim.pk)
    if claim.status != ClaimRequest.Status.PENDING_CLEARANCE or (not is_system_admin(request.user) and claim.requester_id != request.user.id):
        messages.info(request, "รายการนี้ถูกดำเนินการหรือส่งต่อไปขั้นถัดไปแล้ว ระบบได้อัปเดตสถานะล่าสุดให้คุณ")
        return redirect("claim_detail", claim_id=claim.id)
    actual_total = Decimal("0.00")
    item_values = []
    for item in claim.items.all():
        try:
            value = Decimal(request.POST.get(f"actual_{item.id}", "").strip())
            if value < 0 or value.as_tuple().exponent < -2:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            messages.error(request, f"กรุณาระบุยอดใช้จริงของรายการ {item.description} ให้ถูกต้อง")
            return redirect("claim_detail", claim_id=claim.id)
        actual_total += value
        item_values.append((item, value))
    completion_attachments = request.FILES.getlist("attachments")
    if not completion_attachments and not claim.attachments.exists():
        messages.error(request, "กรุณาแนบเอกสารประกอบยอดใช้จริงอย่างน้อย 1 ไฟล์")
        return redirect("claim_detail", claim_id=claim.id)
    errors = attachment_errors(completion_attachments, claim.attachments.count())
    if errors:
        messages.error(request, errors[0])
        return redirect("claim_detail", claim_id=claim.id)
    before = claim.status
    with transaction.atomic():
        has_excess = actual_total > claim.requested_total
        claim.status = ClaimRequest.Status.PENDING_EXCESS_APPROVAL if has_excess else ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW
        claim.actual_total = actual_total
        claim.prepare_excess_approval()
        claim.clearance_note = ""
        claim.excess_reason = ""
        claim.save(update_fields=["status", "actual_total", "clearance_note", "excess_reason", "excess_daily_total", "excess_exposure_details", "excess_cfo_required", "excess_accounting_approved_at", "excess_cfo_approved_at", "cfo_excess_acknowledged_at", "updated_at"])
        for item, value in item_values:
            item.actual_amount = value
            item.actual_description = ""
            item.save(update_fields=["actual_amount", "actual_description"])
        add_approval_record(
            claim,
            request.user,
            ClaimApproval.Action.EXCESS_SUBMITTED if has_excess else ClaimApproval.Action.SUBMITTED,
            before,
            f"ผู้ขอลงยอดใช้จริง {actual_total:,.2f} บาท",
        )
        for upload in completion_attachments:
            ClaimAttachment.objects.create(
                claim_request=claim, file=upload,
                original_name=upload.name, uploaded_by=request.user,
            )
    log_event(request.user, "RECORD_ACTUAL", claim.document_number or f"คำขอ #{claim.id}", f"ยอดใช้จริง {actual_total:,.2f} บาท")
    if has_excess:
        notify_next_approvers(claim, before)
        messages.success(request, "ส่งยอดใช้จริงให้ผู้จัดการบัญชีอนุมัติส่วนต่างแล้ว")
    else:
        notify(claim.accountant, "รอตรวจยอดใช้จริง", f"คำขอ {claim.document_number} ผู้ขอลงยอดใช้จริงแล้ว กรุณาตรวจและปิดรายการ", claim)
        messages.success(request, "ส่งยอดใช้จริงให้บัญชีตรวจแล้ว")
    return redirect("claim_detail", claim_id=claim.id)


@login_required
@transaction.atomic
def claim_amount_correct(request, claim_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    visible = visible_claim_or_404(request.user, claim_id)
    User.objects.select_for_update().get(pk=visible.requester_id)
    claim = ClaimRequest.objects.select_for_update().get(pk=claim_id)
    if request.user.pk == claim.requester_id or (not is_system_admin(request.user) and claim.accountant_id != request.user.pk):
        return HttpResponseForbidden("เฉพาะบัญชีผู้รับผิดชอบที่ไม่ใช่ผู้ขอเท่านั้น")
    if not claim.can_correct_before_payment:
        messages.error(request, "แก้ยอดนี้ได้เฉพาะเบิกออกก่อนที่ยังไม่บันทึกจ่ายเงิน")
        return redirect("claim_detail", claim_id=claim.pk)
    if request.POST.get("amount_revision") != str(claim.amount_revision):
        messages.error(request, "ยอดเปลี่ยนแล้ว กรุณาเปิดข้อมูลล่าสุดก่อนแก้ไข")
        return redirect("claim_detail", claim_id=claim.pk)
    items = list(claim.items.select_for_update().all())
    corrected, changes = [], []
    total = Decimal('0.00')
    for item in items:
        try:
            value = Decimal(request.POST.get(f'corrected_requested_{item.pk}', '').strip())
            if not value.is_finite() or value <= 0 or value.as_tuple().exponent < -2 or value >= Decimal('10000000000'):
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            messages.error(request, f'ยอดรายการ {item.description} ไม่ถูกต้อง ต้องมากกว่า 0 และไม่เกินสองตำแหน่งทศนิยม')
            return redirect('claim_detail', claim_id=claim.pk)
        total += value
        corrected.append((item, value))
        if value != item.requested_amount:
            changes.append(f'{item.description}: {item.requested_amount:,.2f} → {value:,.2f}')
    if not items or total >= Decimal('10000000000'):
        messages.error(request, 'ยอดรวมไม่ถูกต้องหรือเกินวงเงินที่ระบบรองรับ')
        return redirect('claim_detail', claim_id=claim.pk)
    if not changes:
        messages.info(request, 'ยอดไม่เปลี่ยนแปลง')
        return redirect('claim_detail', claim_id=claim.pk)
    old_total, before = claim.requested_total, claim.status
    previously_required_cfo = claim.requires_cfo_approval or claim.cfo_approved_at is not None
    claim.requested_total = total
    claim.capture_exposure()
    claim.corrected_cfo_required = previously_required_cfo or claim.requires_cfo_approval
    if claim.corrected_cfo_required:
        if not claim.accounting_manager_id or not claim.cfo_id or claim.cfo_id == claim.accounting_manager_id:
            messages.error(request, 'กรุณาตั้งผู้จัดการบัญชีและ CFO คนละคนให้ครบก่อนแก้ยอด')
            return redirect('claim_detail', claim_id=claim.pk)
        claim.status = ClaimRequest.Status.PENDING_ACCOUNTING
        claim.accounting_approved_at = None
        claim.cfo_approved_at = None
        claim.cfo_acknowledged_at = None
    for item, value in corrected:
        item.requested_amount = value
        item.save(update_fields=['requested_amount'])
    claim.amount_revision += 1
    claim.save(update_fields=['requested_total', 'daily_requested_total', 'approval_exposure_details', 'corrected_cfo_required', 'amount_revision',
        'status', 'accounting_approved_at', 'cfo_approved_at', 'cfo_acknowledged_at', 'updated_at'])
    detail = f'แก้ยอดก่อนจ่าย {old_total:,.2f} → {total:,.2f} บาท; ' + '; '.join(changes)
    note = request.POST.get('correction_note', '').strip()
    if note:
        detail += f'; หมายเหตุ: {note}'
    add_approval_record(claim, request.user, ClaimApproval.Action.AMOUNT_CORRECTED, before, detail)
    log_event(request.user, 'CORRECT_REQUESTED', claim.document_number or f'คำขอ #{claim.pk}', detail)
    notify(claim.requester, 'บัญชีแก้ยอดก่อนจ่าย', detail, claim)
    if claim.status == ClaimRequest.Status.PENDING_ACCOUNTING:
        notify_next_approvers(claim, before)
        messages.success(request, 'แก้ยอดแล้ว รอผู้จัดการบัญชีและ CFO อนุมัติใหม่ก่อนจ่าย')
    else:
        messages.success(request, 'แก้ยอดแล้ว กรุณาตรวจยอดล่าสุดก่อนบันทึกจ่ายเงิน')
    return redirect('claim_detail', claim_id=claim.pk)


@login_required
@transaction.atomic
def claim_clearance_correct(request, claim_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    visible_claim = visible_claim_or_404(request.user, claim_id)
    User.objects.select_for_update().get(pk=visible_claim.requester_id)
    claim = ClaimRequest.objects.select_for_update().get(pk=claim_id)
    if not is_system_admin(request.user) and claim.accountant_id != request.user.id:
        return HttpResponseForbidden("เฉพาะเจ้าหน้าที่บัญชีที่รับผิดชอบคำขอนี้เท่านั้น")
    can_correct = claim.request_type == ClaimRequest.RequestType.ADVANCE and (
        claim.status == ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW
        or (claim.status == ClaimRequest.Status.PENDING_PAYMENT and claim.has_excess)
    )
    if not can_correct:
        messages.error(request, "แก้ยอดใช้จริงได้เฉพาะขั้นตรวจยอดหรือก่อนจ่ายส่วนต่าง")
        return redirect("claim_detail", claim_id=claim.id)

    items = list(claim.items.select_for_update().all())
    corrected = []
    total = Decimal("0.00")
    for item in items:
        try:
            value = Decimal(request.POST.get(f"corrected_actual_{item.pk}", "").strip())
            if not value.is_finite() or value < 0 or value.as_tuple().exponent < -2 or value >= Decimal("10000000000"):
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            messages.error(request, f"กรุณาระบุยอดใช้จริงของรายการ {item.description} ให้ถูกต้อง")
            return redirect("claim_detail", claim_id=claim.id)
        total += value
        corrected.append((item, value))
    if total >= Decimal("10000000000"):
        messages.error(request, "ยอดใช้จริงรวมสูงเกินกว่าที่ระบบรองรับ")
        return redirect("claim_detail", claim_id=claim.id)
    changes = [
        f"{item.description}: {item.actual_amount:,.2f} → {value:,.2f} บาท"
        for item, value in corrected if item.actual_amount != value
    ]
    if not changes:
        messages.info(request, "ยอดใช้จริงไม่เปลี่ยนแปลง")
        return redirect("claim_detail", claim_id=claim.id)

    old_total = claim.actual_total
    before = claim.status
    for item, value in corrected:
        if item.actual_amount != value:
            item.actual_amount = value
            item.save(update_fields=["actual_amount"])
    claim.actual_total = total
    if before == ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW and total > claim.requested_total:
        # A newly created excess still needs its first accounting approval.
        claim.status = ClaimRequest.Status.PENDING_EXCESS_APPROVAL
        claim.prepare_excess_approval()
    elif before == ClaimRequest.Status.PENDING_PAYMENT and total <= claim.requested_total:
        # No supplemental payment remains after the correction.
        claim.status = ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW
    claim.save(update_fields=["actual_total", "status", "excess_daily_total", "excess_exposure_details", "excess_cfo_required", "excess_accounting_approved_at", "excess_cfo_approved_at", "cfo_excess_acknowledged_at", "updated_at"])
    note = request.POST.get("correction_note", "").strip()
    detail = f"แก้ยอดใช้จริง {old_total:,.2f} → {total:,.2f} บาท; " + "; ".join(changes)
    if note:
        detail += f"; หมายเหตุ: {note}"
    add_approval_record(claim, request.user, ClaimApproval.Action.ACTUAL_CORRECTED, before, detail)
    log_event(request.user, "CORRECT_ACTUAL", claim.document_number or f"คำขอ #{claim.id}", detail)
    if claim.status == ClaimRequest.Status.PENDING_EXCESS_APPROVAL:
        notify(claim.accounting_manager, "รออนุมัติส่วนต่าง", f"คำขอ {claim.document_number} บัญชีแก้ยอดใช้จริงเป็น {total:,.2f} บาท กรุณาอนุมัติส่วนต่าง", claim)
    notify(claim.requester, "บัญชีแก้ยอดใช้จริง", f"คำขอ {claim.document_number} ปรับยอดใช้จริงจาก {old_total:,.2f} เป็น {total:,.2f} บาท", claim)
    messages.success(request, "บันทึกยอดใช้จริงที่แก้ไขแล้ว")
    return redirect("claim_detail", claim_id=claim.id)


@login_required
def claim_complete(request, claim_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claim = visible_claim_or_404(request.user, claim_id)
    if claim.status != ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW or (not is_system_admin(request.user) and claim.accountant_id != request.user.id):
        messages.info(request, "รายการนี้ไม่อยู่ในขั้นตรวจยอดใช้จริงของบัญชี")
        return redirect("claim_detail", claim_id=claim.id)
    if claim.actual_total < claim.requested_total and request.POST.get("refund_confirmed") != "1":
        messages.error(request, "กรุณายืนยันว่าได้รับเงินส่วนที่เหลือคืนแล้ว")
        return redirect("claim_detail", claim_id=claim.id)
    before = claim.status
    claim.status = ClaimRequest.Status.COMPLETED
    claim.save(update_fields=["status", "updated_at"])
    add_approval_record(claim, request.user, ClaimApproval.Action.COMPLETED, before, "บัญชีตรวจยอดใช้จริงและยืนยันการเคลียร์เงินแล้ว")
    log_event(request.user, "COMPLETE_CLAIM", claim.document_number or f"คำขอ #{claim.id}")
    notify(claim.requester, "คำขอเสร็จสิ้น", f"คำขอ {claim.document_number} ตรวจยอดใช้จริงและปิดรายการแล้ว", claim)
    messages.success(request, "ตรวจสอบและปิดรายการเรียบร้อยแล้ว")
    return redirect("claim_detail", claim_id=claim.id)


@login_required
def claim_cancel_after_refund(request, claim_id):
    """Accountant closes a paid request when the full advance has been returned unused."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claim = visible_claim_or_404(request.user, claim_id)
    if claim.status != ClaimRequest.Status.PENDING_CLEARANCE or (not is_system_admin(request.user) and claim.accountant_id != request.user.id):
        messages.info(request, "รายการนี้ไม่อยู่ในขั้นตอนที่ยกเลิกหลังรับเงินคืนได้")
        return redirect("claim_detail", claim_id=claim.id)
    reason = request.POST.get("cancel_reason", "").strip()
    if not request.POST.get("refund_confirmed"):
        messages.error(request, "กรุณายืนยันว่าได้รับเงินคืนครบจำนวนก่อนยกเลิกรายการ")
        return redirect("claim_detail", claim_id=claim.id)
    if not reason:
        messages.error(request, "กรุณาระบุเหตุผลการยกเลิกรายการ")
        return redirect("claim_detail", claim_id=claim.id)
    before = claim.status
    claim.status = ClaimRequest.Status.CANCELLED
    claim.save(update_fields=["status", "updated_at"])
    comment = f"ได้รับเงินคืนครบจำนวน {claim.requested_total:,.2f} บาท — {reason}"
    add_approval_record(claim, request.user, ClaimApproval.Action.CANCELLED, before, comment)
    log_event(request.user, "CANCEL_CLAIM", claim.document_number or f"คำขอ #{claim.id}", comment)
    notify(claim.requester, "คำขอถูกยกเลิก", f"บัญชีได้รับเงินคืนครบจำนวน {claim.requested_total:,.2f} บาทแล้ว: {reason}", claim)
    messages.success(request, "ยืนยันรับเงินคืนครบจำนวนและยกเลิกคำขอเรียบร้อยแล้ว")
    return redirect("claim_detail", claim_id=claim.id)


@login_required
def claim_print(request, claim_id):
    claim = visible_claim_or_404(request.user, claim_id)
    if not get_system_settings().claim_print_enabled:
        raise Http404("ปิดการใช้งานเอกสารสำหรับพิมพ์")
    if claim.status != ClaimRequest.Status.COMPLETED:
        raise Http404("พิมพ์ได้เฉพาะรายการที่เสร็จสิ้นแล้ว")
    difference = claim.requested_total - (claim.actual_total or Decimal("0.00"))
    return render(request, "claims/claim_print.html", {"claim": claim, "final_signoffs": claim_print_final_signoffs(claim), "difference": difference, "has_excess": claim.has_excess, "settlement_amount": claim.settlement_amount, "total_words": thai_baht_text(claim.requested_total)})


def claim_print_final_signoffs(claim):
    """Show one final text sign-off per role from the effective approval round."""
    records = list(claim.approval_records.select_related('actor').order_by('created_at', 'pk'))
    start = max((i for i, record in enumerate(records)
                 if record.action == ClaimApproval.Action.SUBMITTED
                 and record.status_before in (ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED)), default=0)
    records = records[start:]
    # A correction can require fresh accounting/CFO approvals without repeating the head's approval.
    correction = max((i for i, record in enumerate(records)
                      if record.action == ClaimApproval.Action.AMOUNT_CORRECTED
                      and record.status_after == ClaimRequest.Status.PENDING_ACCOUNTING), default=-1)
    excess_start = max((i for i, record in enumerate(records)
                        if record.action == ClaimApproval.Action.EXCESS_SUBMITTED), default=-1)
    latest = {}
    supplemental = False
    roles = {
        ClaimRequest.Status.PENDING_BUDGET_APPROVAL: 'อนุมัติ Budget',
        ClaimRequest.Status.PENDING_SUPERVISOR: 'หัวหน้าอนุมัติ',
        ClaimRequest.Status.PENDING_EXECUTIVE: 'ผู้บริหารอนุมัติ',
        ClaimRequest.Status.PENDING_ACCOUNTING: 'ผู้จัดการบัญชีอนุมัติ',
        ClaimRequest.Status.PENDING_EXCESS_APPROVAL: 'ผู้จัดการบัญชีอนุมัติส่วนต่าง',
    }
    for index, record in enumerate(records):
        action = record.action
        if action == ClaimApproval.Action.SUPPLEMENTAL_PAID:
            supplemental = True
        if action == ClaimApproval.Action.APPROVED:
            if record.status_before == ClaimRequest.Status.PENDING_ACCOUNTING and index < correction:
                continue
            if record.status_before == ClaimRequest.Status.PENDING_EXCESS_APPROVAL and index < excess_start:
                continue
            label = roles.get(record.status_before, 'อนุมัติ')
            if record.actor_id == claim.cfo_id and record.status_before in (ClaimRequest.Status.PENDING_ACCOUNTING, ClaimRequest.Status.PENDING_EXCESS_APPROVAL):
                label = 'CFO อนุมัติส่วนต่าง' if record.status_before == ClaimRequest.Status.PENDING_EXCESS_APPROVAL else 'CFO อนุมัติ'
            key = (label, record.actor_id if record.status_before == ClaimRequest.Status.PENDING_BUDGET_APPROVAL else None)
        elif action == ClaimApproval.Action.ACKNOWLEDGED:
            is_excess = 'ยอดใช้เกิน' in record.comment or record.status_before == ClaimRequest.Status.PENDING_EXCESS_APPROVAL
            if is_excess and index < excess_start:
                continue
            if not is_excess and index < correction:
                continue
            label = 'CFO รับทราบส่วนต่าง' if is_excess else 'CFO รับทราบ'
            key = (label, None)
        else:
            label = {
                ClaimApproval.Action.PAID: 'จ่ายเงินตามคำขอ',
                ClaimApproval.Action.SUPPLEMENTAL_PAID: 'จ่ายเงินส่วนต่าง',
                ClaimApproval.Action.RECEIVED: 'ยืนยันรับเงินส่วนต่าง' if supplemental else 'ยืนยันรับเงิน',
                ClaimApproval.Action.AUTO_RECEIVED: 'รับเงินส่วนต่างอัตโนมัติ' if supplemental else 'รับเงินอัตโนมัติ',
                ClaimApproval.Action.COMPLETED: 'ตรวจสอบและปิดรายการ',
            }.get(action)
            if action == ClaimApproval.Action.SUBMITTED:
                label = 'ยื่นคำขอ' if record.status_before in (ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED) else 'ส่งยอดใช้จริง'
            if action == ClaimApproval.Action.EXCESS_SUBMITTED:
                label = 'ส่งยอดใช้จริงเพื่ออนุมัติส่วนต่าง'
            if not label:
                continue
            key = ('receipt' if action in (ClaimApproval.Action.RECEIVED, ClaimApproval.Action.AUTO_RECEIVED) else label, supplemental)
        latest[key] = {'label': label,
                       'name': (record.actor.get_full_name() or record.actor.username) if record.actor else 'ระบบอัตโนมัติ',
                       'actor_id': record.actor_id,
                       'at': record.created_at, 'pk': record.pk,
                       'automatic': action == ClaimApproval.Action.AUTO_RECEIVED}
    # Print a compact final sign-off per role, rather than an event timeline.
    final_roles = {
        'ยื่นคำขอ': ('requester', 'ผู้ขอเบิก', 0),
        'อนุมัติ Budget': ('budget', 'ผู้อนุมัติ Budget', 1),
        'หัวหน้าอนุมัติ': ('supervisor', 'ผู้ตรวจสอบ', 2),
        'ผู้บริหารอนุมัติ': ('executive', 'ผู้บริหาร', 2),
        'ผู้จัดการบัญชีอนุมัติ': ('manager', 'ผู้อนุมัติ', 3),
        'ผู้จัดการบัญชีอนุมัติส่วนต่าง': ('manager', 'ผู้อนุมัติ', 3),
        'CFO อนุมัติ': ('cfo', 'CFO ผู้อนุมัติ', 4),
        'CFO อนุมัติส่วนต่าง': ('cfo', 'CFO ผู้อนุมัติ', 4),
        'จ่ายเงินตามคำขอ': ('payer', 'ผู้จ่ายเงิน', 5),
        'จ่ายเงินส่วนต่าง': ('payer', 'ผู้จ่ายเงิน', 5),
        'ยืนยันรับเงิน': ('receiver', 'ผู้รับเงิน', 6),
        'ยืนยันรับเงินส่วนต่าง': ('receiver', 'ผู้รับเงิน', 6),
        'รับเงินอัตโนมัติ': ('receiver', 'ผู้รับเงิน', 6),
        'รับเงินส่วนต่างอัตโนมัติ': ('receiver', 'ผู้รับเงิน', 6),
    }
    final = {}
    for row in sorted(latest.values(), key=lambda row: (row['at'], row['pk'])):
        role = final_roles.get(row['label'])
        if not role:
            continue
        key, label, order = role
        if key == 'budget':
            key = (key, row['actor_id'])
        final[key] = {**row, 'label': label, 'order': order}
    if 'requester' not in final:
        final['requester'] = {'label': 'ผู้ขอเบิก',
                              'name': claim.requester.get_full_name() or claim.requester.username,
                              'at': None, 'automatic': False, 'order': 0}
    return sorted(final.values(), key=lambda row: row['order'])


def claim_print_signers(claim):
    records = list(claim.approval_records.select_related("actor").all())
    approved_records = [record for record in records if record.action == ClaimApproval.Action.APPROVED]

    def signer(user, record=None):
        return {"user": user, "signed_at": record.created_at if record else None,
                "display_name": (user.get_full_name() or user.username) if user else "",
                "signature_url": reverse("claim_signature_image", args=[claim.pk, user.pk]) if user and user.signature else ""}

    submitted = next((record for record in records if record.action == ClaimApproval.Action.SUBMITTED), None)
    paid = next((record for record in records if record.action == ClaimApproval.Action.PAID), None)
    latest_receipt = next((record for record in reversed(records) if record.action in (ClaimApproval.Action.RECEIVED, ClaimApproval.Action.AUTO_RECEIVED)), None)
    received = latest_receipt if latest_receipt and latest_receipt.action == ClaimApproval.Action.RECEIVED else None
    auto_received = latest_receipt if latest_receipt and latest_receipt.action == ClaimApproval.Action.AUTO_RECEIVED else None
    completed = next((record for record in records if record.action == ClaimApproval.Action.COMPLETED), None)
    supplemental_paid = next((record for record in records if record.action == ClaimApproval.Action.SUPPLEMENTAL_PAID), None)
    supervisor_approval = next((record for record in approved_records if record.status_before == ClaimRequest.Status.PENDING_SUPERVISOR), None)
    executive_approval = next((record for record in approved_records if record.status_before == ClaimRequest.Status.PENDING_EXECUTIVE), None)
    manager_approval = next((record for record in approved_records if record.status_before == ClaimRequest.Status.PENDING_ACCOUNTING and record.actor_id == claim.accounting_manager_id), None)
    first_approval = supervisor_approval or executive_approval
    signers = {
        "requester": signer(claim.requester, submitted),
        "supervisor": signer(first_approval.actor if first_approval else claim.supervisor, first_approval),
        "manager": signer(claim.accounting_manager, manager_approval),
        "accountant": signer((supplemental_paid or paid).actor if (supplemental_paid or paid) else claim.accountant, supplemental_paid or paid or completed),
        "receiver": signer(received.actor if received else (None if auto_received else claim.requester), received),
    }
    return signers, auto_received, bool(executive_approval and not supervisor_approval)


@login_required
def claim_signature_image(request, claim_id, user_id):
    from apps.accounts.signatures import signature_response
    claim = visible_claim_or_404(request.user, claim_id)
    if not get_system_settings().claim_print_enabled or claim.status != ClaimRequest.Status.COMPLETED:
        raise Http404
    signers, _, _ = claim_print_signers(claim)
    for signer in signers.values():
        if signer["user"] and signer["user"].pk == user_id:
            return signature_response(signer["user"])
    raise Http404


@login_required
def claim_attachment_download(request, claim_id, attachment_id):
    claim = visible_claim_or_404(request.user, claim_id)
    try:
        attachment = claim.attachments.get(pk=attachment_id)
    except ClaimAttachment.DoesNotExist:
        raise Http404("ไม่พบไฟล์แนบ")
    return FileResponse(attachment.file.open("rb"), as_attachment=True, filename=attachment.original_name)


@login_required
@xframe_options_sameorigin
def claim_attachment_preview(request, claim_id, attachment_id):
    claim = visible_claim_or_404(request.user, claim_id)
    try:
        attachment = claim.attachments.get(pk=attachment_id)
    except ClaimAttachment.DoesNotExist:
        raise Http404("ไม่พบไฟล์แนบ")
    content_type = mimetypes.guess_type(attachment.original_name)[0] or "application/octet-stream"
    return FileResponse(attachment.file.open("rb"), as_attachment=False, filename=attachment.original_name, content_type=content_type)


@login_required
@xframe_options_sameorigin
def claim_reference_attachment(request, claim_id, source_id, attachment_id, download=False):
    claim = visible_claim_or_404(request.user, claim_id)
    if not can_view_references(request.user, claim):
        raise Http404('ไม่มีสิทธิ์ดูเอกสารประกอบยอด')
    if not any(row.get('id') == source_id and not row.get('own') for row in (exposure_snapshot(claim) or [])):
        raise Http404('เอกสารนี้ไม่ได้ใช้ประกอบยอดคำขอ')
    attachment = get_object_or_404(ClaimAttachment, pk=attachment_id,
        claim_request_id=source_id, claim_request__requester_id=claim.requester_id)
    content_type = mimetypes.guess_type(attachment.original_name)[0] or 'application/octet-stream'
    if not download and content_type not in ('application/pdf', 'image/png', 'image/jpeg'):
        raise Http404('ไม่รองรับพรีวิวไฟล์ชนิดนี้')
    try:
        file = attachment.file.open('rb')
    except FileNotFoundError:
        raise Http404('ไม่พบไฟล์เอกสาร')
    response = FileResponse(file, as_attachment=download, filename=attachment.original_name, content_type=content_type)
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@login_required
def claim_payment_slip_download(request, claim_id, payment_id):
    return payment_slip_response(request, claim_id, payment_id, as_attachment=True)


@login_required
@xframe_options_sameorigin
def claim_payment_slip_preview(request, claim_id, payment_id):
    return payment_slip_response(request, claim_id, payment_id, as_attachment=False)


def payment_slip_response(request, claim_id, payment_id, *, as_attachment):
    claim = visible_claim_or_404(request.user, claim_id)
    if not can_view_claim_payments(request.user, claim):
        raise Http404("ไม่พบสลิปการจ่ายเงิน")
    try:
        payment = claim.payments.get(pk=payment_id, method=ClaimPayment.Method.TRANSFER)
    except ClaimPayment.DoesNotExist:
        raise Http404("ไม่พบสลิปการจ่ายเงิน")
    if not payment.slip:
        raise Http404("ไม่พบสลิปการจ่ายเงิน")
    filename = payment.slip_original_name or payment.slip.name.rsplit('/', 1)[-1]
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if not as_attachment and content_type not in ('application/pdf', 'image/jpeg', 'image/png'):
        raise Http404("ไม่รองรับการพรีวิวไฟล์นี้")
    try:
        file = payment.slip.open("rb")
    except FileNotFoundError:
        raise Http404("ไม่พบไฟล์สลิปการจ่ายเงิน")
    response = FileResponse(file, as_attachment=as_attachment, filename=filename, content_type=content_type)
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@login_required
def approval_queue(request):
    pending_statuses = (
        ClaimRequest.Status.PENDING_BUDGET_APPROVAL,
        ClaimRequest.Status.PENDING_SUPERVISOR,
        ClaimRequest.Status.PENDING_EXECUTIVE,
        ClaimRequest.Status.PENDING_ACCOUNTING,
        ClaimRequest.Status.PENDING_PAYMENT,
        ClaimRequest.Status.PENDING_CLEARANCE,
        ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW,
        ClaimRequest.Status.PENDING_EXCESS_APPROVAL,
    )
    claims = ClaimRequest.objects.filter(status__in=pending_statuses).select_related(
        "requester", "supervisor", "executive", "accounting_manager", "accountant", "cfo"
    )
    if not is_system_admin(request.user):
        claims = claims.filter(
            Q(budget_approvals__approver=request.user, budget_approvals__approved_at__isnull=True, status=ClaimRequest.Status.PENDING_BUDGET_APPROVAL)
            | Q(supervisor=request.user, status=ClaimRequest.Status.PENDING_SUPERVISOR)
            | Q(executive=request.user, status=ClaimRequest.Status.PENDING_EXECUTIVE)
            | Q(accounting_manager=request.user, status=ClaimRequest.Status.PENDING_ACCOUNTING, accounting_approved_at__isnull=True)
            | Q(accounting_manager=request.user, status=ClaimRequest.Status.PENDING_EXCESS_APPROVAL, excess_accounting_approved_at__isnull=True)
            | (Q(cfo=request.user) & pending_cfo_approval_q())
            | Q(accountant=request.user, status__in=(ClaimRequest.Status.PENDING_PAYMENT, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW))
            | Q(requester=request.user, status__in=(ClaimRequest.Status.PAID, ClaimRequest.Status.PENDING_CLEARANCE))
        )
    search_query = request.GET.get("q", "").strip()
    selected_status = request.GET.get("status", "")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    allowed_statuses = set(claims.values_list("status", flat=True).distinct())
    if selected_status not in allowed_statuses:
        selected_status = ""
    if search_query:
        claims = claims.filter(
            Q(document_number__icontains=search_query)
            | Q(purpose__icontains=search_query)
            | Q(requester__username__icontains=search_query)
            | Q(requester__first_name__icontains=search_query)
            | Q(requester__last_name__icontains=search_query)
        )
    if selected_status:
        claims = claims.filter(status=selected_status)
    if date_from:
        claims = claims.filter(request_date__gte=date_from)
    if date_to:
        claims = claims.filter(request_date__lte=date_to)
    claims, page_obj, page_size, query_params = paginate(request, claims.order_by("-updated_at"))
    return render(request, "claims/approval_queue.html", {
        "claims": claims, "page_obj": page_obj, "page_size": page_size, "query_params": query_params,
        "users": [], "audit_logs": [],
        "search_query": search_query,
        "selected_status": selected_status,
        "date_from": date_from,
        "date_to": date_to,
        "status_choices": [(code, label) for code, label in ClaimRequest.Status.choices if code in allowed_statuses],
    })


@login_required
def claim_delete(request, claim_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claim = owned_claim_or_404(request.user, claim_id)
    if is_system_admin(request.user):
        audit_target = claim.document_number or f"คำขอ #{claim.id}"
        claim_link = reverse("claim_detail", kwargs={"claim_id": claim.id})
        # File fields are not removed automatically by Django's database
        # cascade, so remove attached files before deleting the request.
        for attachment in claim.attachments.all():
            attachment.file.delete(save=False)
        Notification.objects.filter(link=claim_link).delete()
        claim.delete()
        log_event(request.user, "DELETE_CLAIM", audit_target)
        messages.success(request, "ลบคำขอและข้อมูลที่เกี่ยวข้องเรียบร้อยแล้ว")
        return redirect("claim_list")
    if claim.status != ClaimRequest.Status.DRAFT:
        messages.error(request, "ลบได้เฉพาะแบบร่างที่ยังไม่ส่งอนุมัติ")
        return redirect("claim_detail", claim_id=claim.id)
    audit_target = claim.document_number or f"คำขอ #{claim.id}"
    claim.delete()
    log_event(request.user, "DELETE_CLAIM", audit_target)
    messages.success(request, "ลบแบบร่างเรียบร้อยแล้ว")
    return redirect("claim_list")


@login_required
def claim_attachment_delete(request, claim_id, attachment_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claim = owned_claim_or_404(request.user, claim_id)
    if claim.status not in (ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED):
        messages.error(request, "ลบไฟล์ได้เฉพาะแบบร่างหรือรายการที่ส่งกลับแก้ไข")
        return redirect("claim_detail", claim_id=claim.id)
    try:
        attachment = claim.attachments.get(pk=attachment_id)
    except ClaimAttachment.DoesNotExist:
        raise Http404("ไม่พบไฟล์แนบ")
    attachment.file.delete(save=False)
    attachment.delete()
    log_event(request.user, "DELETE_ATTACHMENT", claim.document_number or f"คำขอ #{claim.id}", attachment.original_name)
    messages.success(request, "ลบไฟล์แนบเรียบร้อยแล้ว")
    return redirect("claim_edit", claim_id=claim.id)
