from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from .secret_fields import SecretTextField


AUDIT_ACTION_LABELS = {
    "CORRECT_REQUESTED": "บัญชีแก้ยอดก่อนจ่าย",
    "LOGIN_LOCAL": "เข้าสู่ระบบ (บัญชีภายใน)", "LOGIN_AD": "เข้าสู่ระบบ (Active Directory)",
    "CREATE_USER": "สร้างผู้ใช้", "UPDATE_USER": "แก้ไขผู้ใช้", "ENABLE_USER": "เปิดใช้งานผู้ใช้", "DISABLE_USER": "ปิดใช้งานผู้ใช้", "DELETE_USER": "ลบผู้ใช้",
    "CREATE_CLAIM": "สร้างคำขอ", "UPDATE_CLAIM": "แก้ไขคำขอ", "SUBMIT_CLAIM": "ส่งคำขออนุมัติ", "APPROVE_CLAIM": "อนุมัติคำขอ", "ACKNOWLEDGE_CLAIM": "CFO รับทราบคำขอ", "RETURN_CLAIM": "ส่งกลับแก้ไข", "REJECT_CLAIM": "ไม่อนุมัติคำขอ",
    "PAY_CLAIM": "บันทึกจ่ายเงิน", "CONFIRM_RECEIPT": "ยืนยันรับเงิน", "RECORD_ACTUAL": "บันทึกยอดใช้จริง", "COMPLETE_CLAIM": "ปิดรายการ", "CANCEL_CLAIM": "ยกเลิกคำขอ (รับเงินคืนครบ)", "EXPORT_CLAIMS": "ส่งออกรายการคำขอเป็น Excel", "DELETE_CLAIM": "ลบคำขอ", "DELETE_ATTACHMENT": "ลบไฟล์แนบ",
    "CREATE_BUDGET": "เพิ่ม Budget", "UPDATE_BUDGET": "แก้ไข Budget", "DELETE_BUDGET": "ลบ Budget",
    "CREATE_BU": "เพิ่ม BU", "UPDATE_BU": "แก้ไข BU", "DELETE_BU": "ลบ BU", "IMPORT_BUDGET_MASTER": "นำเข้า BU และ Budget", "CLEAR_TEST_CLAIMS": "ล้างคำขอทดสอบ",
    "DAILY_PENDING_REMINDER": "ส่งแจ้งเตือนงานค้างประจำวัน", "AUTO_CONFIRM_RECEIPT": "ระบบถือว่ารับเงินอัตโนมัติ",
    "UPDATE_SYSTEM_SETTINGS": "แก้ไขการตั้งค่าระบบ", "TEST_DATABASE": "ทดสอบฐานข้อมูล", "TEST_AD": "ทดสอบ AD Gateway", "TEST_TELEGRAM": "ทดสอบ Telegram Bot", "TEST_TELEGRAM_WEBHOOK": "ทดสอบ Telegram Webhook",
    "SET_TELEGRAM_WEBHOOK": "เชื่อมต่อ Telegram Webhook", "UPDATE_SERVER_CONFIGURATION": "แก้ชุดค่าตั้งต้นเซิร์ฟเวอร์", "EXPORT_SERVER_CONFIGURATION": "ดาวน์โหลดชุดค่าตั้งต้นเซิร์ฟเวอร์",
}


class SystemSettings(models.Model):
    """Singleton business settings editable by Super Admin."""
    company_name = models.CharField(max_length=255, default="บริษัท วินโดว์ เอเชีย จำกัด (มหาชน)")
    document_number_prefix = models.CharField(max_length=12, default="BR", validators=[RegexValidator(r"^[A-Za-z0-9]+$", "ใช้ได้เฉพาะตัวอักษรภาษาอังกฤษและตัวเลข")])
    head_office_document_prefix = models.CharField(max_length=12, default="HO", validators=[RegexValidator(r"^[A-Za-z0-9]+$", "ใช้ได้เฉพาะตัวอักษรภาษาอังกฤษและตัวเลข")])
    branch_document_prefix = models.CharField(max_length=12, default="BR", validators=[RegexValidator(r"^[A-Za-z0-9]+$", "ใช้ได้เฉพาะตัวอักษรภาษาอังกฤษและตัวเลข")])
    executive_approval_threshold = models.DecimalField(max_digits=12, decimal_places=2, default=5000)
    cfo = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="cfo_system_settings")
    max_attachment_count = models.PositiveSmallIntegerField(default=5)
    max_attachment_size_mb = models.PositiveSmallIntegerField(default=10)
    telegram_group_chat_id = models.CharField(max_length=64, blank=True)
    telegram_bot_token = SecretTextField(blank=True)
    telegram_webhook_secret = SecretTextField(blank=True)
    server_configuration = SecretTextField(blank=True)
    telegram_enabled = models.BooleanField(default=True)
    require_signature_on_first_login = models.BooleanField(default=True)
    claim_print_enabled = models.BooleanField(default=True)
    auto_receive_enabled = models.BooleanField(default=True)
    auto_receive_after_days = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(365)])
    petty_receive_after_days = models.PositiveSmallIntegerField(default=3, validators=[MinValueValidator(1), MaxValueValidator(365)])
    daily_reminder_time = models.TimeField(default="08:00")
    log_retention_days = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(1)])
    public_base_url = models.URLField(blank=True)
    ad_gateway_url = models.URLField(blank=True)
    ad_app_id = models.CharField(max_length=150, blank=True)
    ad_secret_key = SecretTextField(blank=True)
    ad_origin_ip = models.GenericIPAddressField(blank=True, null=True)
    ad_enabled = models.BooleanField(default=True)
    ciam_enabled = models.BooleanField(default=False)
    ciam_base_url = models.URLField(default="https://ciam.windowasia.com", blank=True)
    ciam_client_id = models.CharField(max_length=150, default="pettycash-spoke-client", blank=True)
    ciam_client_secret = SecretTextField(blank=True)
    ciam_sso_enabled = models.BooleanField(default=False)
    ciam_break_glass_active = models.BooleanField(default=False)
    ciam_auto_provision_group = models.CharField(max_length=100, default="ผู้ขอเบิก", blank=True)
    ciam_session_ttl_minutes = models.PositiveIntegerField(default=480)
    ciam_m2m_key = SecretTextField(blank=True)
    maintenance_mode = models.BooleanField(default=False)
    maintenance_message = models.CharField(max_length=255, default="กำลังปรับปรุงระบบเพื่อให้ใช้งานได้ดียิ่งขึ้น")
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        return super().save(*args, **kwargs)


class Notification(models.Model):
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=200)
    message = models.CharField(max_length=500, blank=True)
    link = models.CharField(max_length=500, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("recipient", "is_read", "created_at"), name="notif_recipient_read_idx")]


class DailyReminderRun(models.Model):
    """One persisted marker prevents the scheduler from notifying twice per day."""
    run_date = models.DateField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-run_date",)


class TelegramWebhookCheck(models.Model):
    """An admin-initiated Telegram callback health check."""
    token = models.CharField(max_length=32, unique=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="telegram_webhook_checks")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)


class BusinessUnit(models.Model):
    """Selectable Distribution Rule from the BU master list."""

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)

    def __str__(self):
        return self.code


class Budget(models.Model):
    """A department Budget name and type, without an allocated amount."""

    class Category(models.TextChoices):
        EXPENSE = "Expense", "Expense"
        ASSET = "Asset", "Asset"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="budgets")
    department = models.CharField(max_length=150, blank=True)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.EXPENSE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("department", "name"), name="unique_department_budget_name")]

    def __str__(self):
        return f"{self.owner} · {self.name}"


class AdminAuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100)
    target = models.CharField(max_length=255, blank=True)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("created_at",), name="audit_created_at_idx")]

    @property
    def label(self):
        return AUDIT_ACTION_LABELS.get(self.action, self.action.replace("_", " ").title())


class TransactionLog(models.Model):
    """
    ISO 27001 Security Audit Trail (v2.12.0 Section 2.2).
    Standardized cross-application audit logging format.
    """
    category = models.CharField(max_length=50)  # 'ciam_sso', 'security_break_glass', 'system_setting', 'password_policy'
    action = models.CharField(max_length=100)
    status = models.CharField(max_length=20, default="success")  # 'success', 'failed', 'warning', 'info'
    message = models.CharField(max_length=500)
    details = models.TextField(blank=True, null=True)  # JSON String
    records_count = models.IntegerField(default=0)
    duration_ms = models.IntegerField(default=0)
    triggered_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("category",), name="trans_log_cat_idx"),
            models.Index(fields=("-created_at",), name="trans_log_created_idx"),
        ]

    def __str__(self):
        return f"[{self.category}] {self.action} ({self.status}) - {self.triggered_by}"
