from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


def claim_attachment_upload_path(instance: "ClaimAttachment", filename: str) -> str:
    extension = Path(filename).suffix.lower()
    return f"claim-attachments/{instance.claim_request_id}/{uuid4().hex}{extension}"


def payment_slip_upload_path(instance: "ClaimPayment", filename: str) -> str:
    extension = Path(filename).suffix.lower()
    return f"payment-slips/{instance.claim_request_id}/{uuid4().hex}{extension}"


class ClaimRequest(models.Model):
    class RequestType(models.TextChoices):
        PETTY_CASH = "PETTY_CASH", "เบิกเงินสดย่อย (ออกเงินไปก่อน)"
        ADVANCE = "ADVANCE", "เบิกยืมเงิน"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "แบบร่าง"
        PENDING_BUDGET_APPROVAL = "PENDING_BUDGET_APPROVAL", "รอเจ้าของ Budget อนุมัติ"
        PENDING_SUPERVISOR = "PENDING_SUPERVISOR", "รอหัวหน้าอนุมัติ"
        PENDING_EXECUTIVE = "PENDING_EXECUTIVE", "รอผู้บริหารอนุมัติ"
        PENDING_ACCOUNTING = "PENDING_ACCOUNTING", "รอผู้จัดการบัญชีอนุมัติ"
        PENDING_PAYMENT = "PENDING_PAYMENT", "รอจ่ายเงิน"
        PAID = "PAID", "จ่ายเงินแล้ว"
        PENDING_CLEARANCE = "PENDING_CLEARANCE", "รอเคลียร์"
        PENDING_ACCOUNTANT_REVIEW = "PENDING_ACCOUNTANT_REVIEW", "รอบัญชีตรวจยอดใช้จริง"
        PENDING_EXCESS_APPROVAL = "PENDING_EXCESS_APPROVAL", "รอผู้จัดการบัญชีอนุมัติส่วนต่าง"
        COMPLETED = "COMPLETED", "เสร็จสิ้น"
        RETURNED = "RETURNED", "ส่งกลับแก้ไข"
        REJECTED = "REJECTED", "ไม่อนุมัติ"
        CANCELLED = "CANCELLED", "ยกเลิก"

    document_number = models.CharField(max_length=32, unique=True, null=True, blank=True)
    request_type = models.CharField(max_length=12, choices=RequestType.choices, default=RequestType.ADVANCE)
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="claim_requests")
    requester_position_level = models.CharField(max_length=12, blank=True)
    approval_threshold = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    supervisor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="supervised_claim_requests")
    executive = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="executive_claim_requests")
    accounting_manager = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="managed_claim_requests")
    accountant = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="assigned_claim_requests")
    cfo = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="cfo_claim_requests")
    accounting_approved_at = models.DateTimeField(null=True, blank=True)
    cfo_approved_at = models.DateTimeField(null=True, blank=True)
    corrected_cfo_required = models.BooleanField(default=False)
    amount_revision = models.PositiveIntegerField(default=0)
    cfo_acknowledged_at = models.DateTimeField(null=True, blank=True)
    cfo_excess_acknowledged_at = models.DateTimeField(null=True, blank=True)
    daily_requested_total = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    excess_daily_total = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    approval_exposure_details = models.JSONField(null=True, blank=True)
    excess_exposure_details = models.JSONField(null=True, blank=True)
    accepted_request_date = models.DateField(null=True, blank=True)
    excess_cfo_required = models.BooleanField(null=True, blank=True)
    excess_accounting_approved_at = models.DateTimeField(null=True, blank=True)
    excess_cfo_approved_at = models.DateTimeField(null=True, blank=True)
    business_unit = models.CharField(max_length=150, blank=True)
    department = models.CharField(max_length=150, blank=True)
    work_location = models.CharField(max_length=10, choices=(('HQ', 'สำนักงานใหญ่'), ('BRANCH', 'กาญจนบุรี'), ('OTHER', 'อื่นๆ')))
    branch_name = models.CharField(max_length=150, blank=True)

    @property
    def location_display(self):
        if self.work_location == "OTHER" and self.branch_name:
            return self.branch_name
        return self.get_work_location_display()
    request_date = models.DateField(default=timezone.localdate)
    expense_month = models.DateField(null=True, blank=True, verbose_name="ค่าใช้จ่ายของเดือน")

    @property
    def expense_month_display(self):
        from apps.core.thai_dates import thai_month_year
        return thai_month_year(self.expense_month)

    budget = models.CharField(max_length=200, blank=True)
    purpose = models.TextField()
    requested_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    actual_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    excess_reason = models.TextField(blank=True)
    clearance_note = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)
        indexes = [
            models.Index(fields=("status", "updated_at"), name="claim_status_updated_idx"),
            models.Index(fields=("requester", "status"), name="claim_requester_status_idx"),
            models.Index(fields=("requester", "request_date"), name="claim_requester_date_idx"),
            models.Index(fields=("request_date",), name="claim_request_date_idx"),
            models.Index(fields=("created_at",), name="claim_created_at_idx"),
        ]

    def __str__(self):
        return self.document_number or f"Draft #{self.pk}"

    def refresh_requested_total(self):
        # The edit view prefetches items, so self.items.all() can still contain
        # the amounts from before the formset saved its changes.
        total = self.items.aggregate(total=models.Sum("requested_amount"))["total"] or Decimal("0.00")
        self.requested_total = total
        self.save(update_fields=["requested_total", "updated_at"])

    def current_approver(self):
        if self.status == self.Status.PENDING_SUPERVISOR:
            return self.supervisor
        if self.status == self.Status.PENDING_EXECUTIVE:
            return self.executive
        if self.status == self.Status.PENDING_EXCESS_APPROVAL:
            if not self.excess_accounting_approved_at:
                return self.accounting_manager
            if self.requires_cfo_excess_approval and not self.excess_cfo_approved_at:
                return self.cfo
            return None
        if self.status == self.Status.PENDING_ACCOUNTING:
            if not self.accounting_approved_at:
                return self.accounting_manager
            if self.requires_cfo_approval and not self.cfo_approved_at:
                return self.cfo
            return None
        if self.status == self.Status.PENDING_PAYMENT:
            return self.accountant
        if self.status == self.Status.PENDING_CLEARANCE:
            return self.requester
        if self.status == self.Status.PENDING_ACCOUNTANT_REVIEW:
            return self.accountant
        return None

    def can_approve(self, user):
        if self.status == self.Status.PENDING_BUDGET_APPROVAL:
            return bool(user and user.pk and self.budget_approvals.filter(approver_id=user.pk, approved_at__isnull=True).exists())
        return bool(user and user.pk and self.current_approver() == user)

    @property
    def workflow_status_display(self):
        if self.status == self.Status.PAID and self.latest_payment_is_supplemental:
            return 'รอยืนยันรับเงินส่วนต่าง'
        if self.status == self.Status.PENDING_ACCOUNTING and self.accounting_approved_at and self.requires_cfo_approval and not self.cfo_approved_at:
            return "รอ CFO อนุมัติ"
        if self.status == self.Status.PENDING_EXCESS_APPROVAL and self.excess_accounting_approved_at and self.requires_cfo_excess_approval and not self.excess_cfo_approved_at:
            return "รอ CFO อนุมัติส่วนต่าง"
        return self.get_status_display()

    def can_cfo_acknowledge(self, user, stage):
        if not user or user.pk != self.cfo_id:
            return False
        if self.status in (self.Status.DRAFT, self.Status.RETURNED, self.Status.REJECTED, self.Status.CANCELLED):
            return False
        if stage == "initial":
            return not self.requires_cfo_approval and not self.requires_cfo_excess_approval and self.accounting_approved_at is not None and self.cfo_acknowledged_at is None
        if stage == "excess":
            return self.has_excess and not self.requires_cfo_excess_approval and self.status in (self.Status.PENDING_PAYMENT, self.Status.PAID, self.Status.COMPLETED) and self.cfo_excess_acknowledged_at is None
        return False

    def can_submit(self):
        return bool(self.expense_month and self.supervisor_id and self.accounting_manager_id and self.accountant_id and self.cfo_id
                    and self.accountant_id != self.requester_id and self.accountant.is_active
                    and self.cfo_id != self.accounting_manager_id and self.items.exists()
                    and (not self.requires_cfo_approval or self.cfo_id)
                    and (self.request_type != self.RequestType.PETTY_CASH or self.attachments.exists()))

    @property
    def requires_executive_approval(self):
        from apps.core.system_settings import get_system_settings
        threshold = self.approval_threshold if self.approval_threshold is not None else get_system_settings().executive_approval_threshold
        return self.requested_total > threshold

    @property
    def requires_cfo_approval(self):
        from apps.accounts.models import User
        level = self.requester_position_level or self.requester.position_level
        return self.corrected_cfo_required or self.requires_executive_approval or (self.daily_requested_total is not None and self.daily_requested_total > self.cfo_threshold) or level in (User.PositionLevel.MANAGER, User.PositionLevel.EXECUTIVE)

    @property
    def can_correct_before_payment(self):
        return (self.request_type == self.RequestType.PETTY_CASH
                and self.status == self.Status.PENDING_PAYMENT and self.actual_total is None
                and not self.payments.exists()
                and not self.approval_records.filter(action__in=(ClaimApproval.Action.PAID, ClaimApproval.Action.SUPPLEMENTAL_PAID)).exists())

    @property
    def cfo_threshold(self):
        from apps.core.system_settings import get_system_settings
        return self.approval_threshold if self.approval_threshold is not None else get_system_settings().executive_approval_threshold

    @property
    def requires_cfo_excess_approval(self):
        if not self.has_excess:
            return False
        if self.excess_cfo_required is not None:
            return self.excess_cfo_required
        return self.requires_cfo_approval or self.actual_total > self.cfo_threshold or (self.excess_daily_total is not None and self.excess_daily_total > self.cfo_threshold)

    def calculate_daily_total(self, *, include_actual=False):
        """Snapshot the submitted exposure for this requester and request date.

        Call while holding the requester's row lock so simultaneous submissions
        cannot each miss the other. Completed claims remain in the daily total.
        """
        return sum((Decimal(row['amount']) for row in self.exposure_components(include_actual=include_actual)), Decimal('0'))

    def exposure_components(self, *, include_actual=False):
        others = type(self).objects.filter(requester_id=self.requester_id).exclude(pk=self.pk).exclude(
            status__in=(self.Status.DRAFT, self.Status.RETURNED, self.Status.REJECTED, self.Status.CANCELLED),
        ).filter(Q(request_date=self.request_date) | ~Q(status=self.Status.COMPLETED)).prefetch_related('payments', 'approval_records').order_by('request_date', 'pk')
        rows = []
        for other in others:
            required = max(other.requested_total, other.actual_total or Decimal('0'))
            if other.request_date == self.request_date:
                amount = other.actual_total if other.status == self.Status.COMPLETED and other.actual_total is not None else required
                basis = 'ยอดวันเดียวกัน: ใช้จริงปิดแล้ว' if other.status == self.Status.COMPLETED and other.actual_total is not None else 'ยอดวันเดียวกัน: ยอดขอ/ใช้เกิน'
            else:
                payments = list(other.payments.all())
                paid = sum((payment.amount for payment in payments), Decimal('0'))
                if not any(payment.kind == ClaimPayment.Kind.INITIAL for payment in payments):
                    funded = (any(record.action in (ClaimApproval.Action.PAID, ClaimApproval.Action.SUPPLEMENTAL_PAID) for record in other.approval_records.all())
                        or other.status in (self.Status.PAID, self.Status.PENDING_CLEARANCE,
                            self.Status.PENDING_ACCOUNTANT_REVIEW, self.Status.PENDING_EXCESS_APPROVAL))
                    if funded:
                        paid += other.requested_total
                amount = max(required - paid, Decimal('0'))
                if not amount:
                    continue
                basis = 'ค้างจ่ายข้ามวัน: ส่วนต่าง' if paid else 'ค้างจ่ายข้ามวัน: เงินต้น'
            rows.append({'id': other.pk, 'document': other.document_number or f'คำขอ #{other.pk}',
                'date': other.request_date.isoformat(), 'amount': str(amount), 'basis': basis})
        own = max(self.requested_total, self.actual_total or Decimal('0')) if include_actual else self.requested_total
        rows.append({'id': self.pk, 'document': self.document_number or f'คำขอ #{self.pk}',
            'date': self.request_date.isoformat(), 'amount': str(own), 'basis': 'ใบปัจจุบัน', 'own': True})
        return rows

    def capture_exposure(self, *, include_actual=False):
        rows = self.exposure_components(include_actual=include_actual)
        total = sum((Decimal(row['amount']) for row in rows), Decimal('0'))
        if include_actual:
            self.excess_exposure_details = rows
            self.excess_daily_total = total
        else:
            self.approval_exposure_details = rows
            self.daily_requested_total = total
        return total

    def calculate_completed_daily_actual_total(self):
        return type(self).objects.filter(
            requester_id=self.requester_id, request_date=self.request_date,
            status=self.Status.COMPLETED, actual_total__isnull=False,
        ).aggregate(total=models.Sum('actual_total'))['total'] or Decimal('0.00')

    def prepare_excess_approval(self):
        self.capture_exposure(include_actual=True)
        self.excess_cfo_required = None
        self.excess_cfo_required = self.requires_cfo_excess_approval
        self.excess_accounting_approved_at = None
        self.excess_cfo_approved_at = None
        self.cfo_excess_acknowledged_at = None

    @property
    def has_excess(self):
        return self.actual_total is not None and self.actual_total > self.requested_total

    @property
    def settlement_amount(self):
        if self.actual_total is None:
            return Decimal("0.00")
        return abs(self.actual_total - self.requested_total)

    @property
    def latest_payment_event(self):
        actions = (ClaimApproval.Action.PAID, ClaimApproval.Action.SUPPLEMENTAL_PAID)
        cached = getattr(self, '_prefetched_objects_cache', {}).get('approval_records')
        if cached is not None:
            return max((record for record in cached if record.action in actions), key=lambda record: (record.created_at, record.pk), default=None)
        return self.approval_records.filter(action__in=actions).order_by('-created_at', '-pk').first()

    @property
    def latest_payment_is_supplemental(self):
        event = self.latest_payment_event
        return event is not None and event.action == ClaimApproval.Action.SUPPLEMENTAL_PAID

    @property
    def receipt_amount(self):
        return self.settlement_amount if self.latest_payment_is_supplemental else self.requested_total

    @property
    def receipt_closes_claim(self):
        return self.request_type == self.RequestType.PETTY_CASH or self.latest_payment_is_supplemental

    def receipt_wait_days(self, configuration):
        if self.latest_payment_is_supplemental:
            return 1
        return configuration.petty_receive_after_days if self.request_type == self.RequestType.PETTY_CASH else configuration.auto_receive_after_days


REQUESTER_STATUS_GROUPS = {
    "in_progress": (
        ClaimRequest.Status.PENDING_BUDGET_APPROVAL,
        ClaimRequest.Status.PENDING_SUPERVISOR,
        ClaimRequest.Status.PENDING_EXECUTIVE,
        ClaimRequest.Status.PENDING_ACCOUNTING,
        ClaimRequest.Status.PENDING_PAYMENT,
        ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW,
        ClaimRequest.Status.PENDING_EXCESS_APPROVAL,
    ),
    "unsuccessful": (ClaimRequest.Status.REJECTED, ClaimRequest.Status.CANCELLED),
}
REQUESTER_STATUS_GROUP_LABELS = {
    "in_progress": "กำลังดำเนินการ",
    "unsuccessful": "ไม่อนุมัติ/ยกเลิก",
}


def pending_cfo_approval_q():
    return Q(status=ClaimRequest.Status.PENDING_ACCOUNTING, accounting_approved_at__isnull=False, cfo_approved_at__isnull=True) | Q(
        status=ClaimRequest.Status.PENDING_EXCESS_APPROVAL, excess_accounting_approved_at__isnull=False, excess_cfo_approved_at__isnull=True,
    )


def pending_cfo_acknowledgement_ids(queryset):
    """Claims with either an initial or excess acknowledgement still due."""
    candidates = (
        queryset.filter(cfo__isnull=False)
        .filter(Q(cfo_acknowledged_at__isnull=True) | Q(cfo_excess_acknowledged_at__isnull=True))
        .exclude(status__in=(
            ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED,
            ClaimRequest.Status.REJECTED, ClaimRequest.Status.CANCELLED,
        ))
        .select_related("requester", "cfo")
    )
    return [
        claim.pk for claim in candidates
        if claim.can_cfo_acknowledge(claim.cfo, "initial")
        or claim.can_cfo_acknowledge(claim.cfo, "excess")
    ]


class ClaimItem(models.Model):
    claim_request = models.ForeignKey(ClaimRequest, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=500)
    business_unit = models.CharField(max_length=150, blank=True)
    department = models.CharField(max_length=150, blank=True)
    budget = models.CharField(max_length=200, blank=True)
    budget_approver = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="budget_approved_items")
    requested_amount = models.DecimalField(max_digits=12, decimal_places=2)
    actual_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_description = models.CharField(max_length=500, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "pk")

    def __str__(self):
        return self.description


class ClaimBudgetApproval(models.Model):
    claim_request = models.ForeignKey(ClaimRequest, on_delete=models.CASCADE, related_name="budget_approvals")
    department = models.CharField(max_length=150)
    approver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="budget_approval_tasks")
    acted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="budget_approval_actions")
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("claim_request", "department", "approver"), name="unique_claim_budget_approver")]


class ClaimAttachment(models.Model):
    claim_request = models.ForeignKey(ClaimRequest, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=claim_attachment_upload_path)
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_claim_attachments")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)

    def __str__(self):
        return self.original_name


class ClaimPayment(models.Model):
    class Method(models.TextChoices):
        TRANSFER = "TRANSFER", "โอนเงิน"
        CASH = "CASH", "เงินสด"

    class Kind(models.TextChoices):
        INITIAL = "INITIAL", "จ่ายเงินตามคำขอ"
        SUPPLEMENTAL = "SUPPLEMENTAL", "จ่ายเงินส่วนต่าง"

    claim_request = models.ForeignKey(ClaimRequest, on_delete=models.CASCADE, related_name="payments")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    method = models.CharField(max_length=8, choices=Method.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    slip = models.FileField(upload_to=payment_slip_upload_path, blank=True)
    slip_original_name = models.CharField(max_length=255, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="recorded_claim_payments")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "pk")


class ClaimDocumentSequence(models.Model):
    prefix = models.CharField(max_length=12)
    period = models.CharField(max_length=6)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('prefix', 'period'), name='unique_claim_monthly_sequence')]


class TelegramApprovalPrompt(models.Model):
    token = models.UUIDField(default=uuid4, unique=True, editable=False)
    claim_request = models.ForeignKey(ClaimRequest, on_delete=models.CASCADE, related_name='telegram_approval_prompts')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    context = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('claim_request', 'actor', 'context'), name='unique_telegram_approval_prompt')]


class TelegramCallbackReceipt(models.Model):
    callback_id = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ClaimApproval(models.Model):
    class Action(models.TextChoices):
        SUBMITTED = "SUBMITTED", "ส่งอนุมัติ"
        APPROVED = "APPROVED", "อนุมัติ"
        ACKNOWLEDGED = "ACKNOWLEDGED", "รับทราบ"
        RETURNED = "RETURNED", "ส่งกลับแก้ไข"
        REJECTED = "REJECTED", "ไม่อนุมัติ"
        PAID = "PAID", "บันทึกจ่ายเงิน"
        RECEIVED = "RECEIVED", "ยืนยันรับเงิน"
        AUTO_RECEIVED = "AUTO_RECEIVED", "ระบบถือว่ารับเงินอัตโนมัติ"
        COMPLETED = "COMPLETED", "ปิดรายการ"
        EXCESS_SUBMITTED = "EXCESS_SUBMITTED", "ส่งอนุมัติส่วนต่าง"
        ACTUAL_CORRECTED = "ACTUAL_CORRECTED", "บัญชีแก้ยอดใช้จริง"
        AMOUNT_CORRECTED = "AMOUNT_CORRECTED", "บัญชีแก้ยอดก่อนจ่าย"
        SUPPLEMENTAL_PAID = "SUPPLEMENTAL_PAID", "จ่ายเงินส่วนต่าง"
        CANCELLED = "CANCELLED", "ยกเลิกคำขอ (รับเงินคืนครบ)"

    claim_request = models.ForeignKey(ClaimRequest, on_delete=models.CASCADE, related_name="approval_records")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="claim_approval_actions")
    action = models.CharField(max_length=20, choices=Action.choices)
    status_before = models.CharField(max_length=30)
    status_after = models.CharField(max_length=30)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
