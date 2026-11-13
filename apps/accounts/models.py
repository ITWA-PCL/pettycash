from pathlib import Path
from uuid import uuid4

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models


def signature_upload_path(instance: "User", filename: str) -> str:
    extension = Path(filename).suffix.lower() or ".png"
    return f"signatures/{instance.pk or 'pending'}/{uuid4().hex}{extension}"


class User(AbstractUser):
    class PositionLevel(models.TextChoices):
        EMPLOYEE = "EMPLOYEE", "พนักงาน"
        MANAGER = "MANAGER", "Manager"
        EXECUTIVE = "EXECUTIVE", "ผู้บริหาร"

    class ThemePreference(models.TextChoices):
        LIGHT = "LIGHT", "โหมดสว่าง"
        DARK = "DARK", "โหมดมืด"

    class AuthSource(models.TextChoices):
        LOCAL = "LOCAL", "บัญชีภายใน"
        AD = "AD", "Active Directory"

    class WorkLocation(models.TextChoices):
        HEAD_OFFICE = "HQ", "สำนักงานใหญ่"
        BRANCH = "BRANCH", "กาญจนบุรี"
        OTHER = "OTHER", "อื่นๆ"

    auth_source = models.CharField(max_length=10, choices=AuthSource.choices, default=AuthSource.LOCAL)
    is_ad_auth = models.BooleanField(default=False, verbose_name="อนุญาต AD Authentication")
    business_unit = models.CharField(max_length=150, blank=True)
    position_level = models.CharField(max_length=12, choices=PositionLevel.choices, default=PositionLevel.EMPLOYEE)
    department = models.CharField(max_length=150, blank=True)
    work_location = models.CharField(max_length=10, choices=WorkLocation.choices, default=WorkLocation.HEAD_OFFICE)
    work_location_other = models.CharField(max_length=150, blank=True)
    supervisor = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="direct_reports",
    )
    executive = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="executive_requesters"
    )
    accounting_manager = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_requesters"
    )
    accountant = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="assigned_requesters"
    )
    telegram_user_id = models.CharField(max_length=32, null=True, blank=True)
    alternate_payer = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="alternate_payment_requesters", verbose_name="ผู้จ่ายสำรองเมื่อเบิกเอง",
        limit_choices_to={"is_active": True, "groups__name": "เจ้าหน้าที่บัญชี"},
    )
    telegram_notifications_enabled = models.BooleanField(default=False)
    signature = models.FileField(upload_to=signature_upload_path, blank=True)
    signature_updated_at = models.DateTimeField(null=True, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True, verbose_name="วันที่เปลี่ยนรหัสผ่านล่าสุด")
    must_change_password = models.BooleanField(default=False, verbose_name="ต้องเปลี่ยนรหัสผ่านในการเข้าใช้งานครั้งถัดไป")
    onboarding_completed = models.BooleanField(default=False)
    ciam_sub = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        unique=True,
        verbose_name="Central IAM Subject Identifier",
    )
    theme_preference = models.CharField(
        max_length=10,
        choices=ThemePreference.choices,
        default=ThemePreference.LIGHT,
    )

    @property
    def is_local_account(self) -> bool:
        """True if this account manages its password locally, not via AD or external SSO."""
        return not self.is_ad_auth and self.auth_source != self.AuthSource.AD

    def check_password_expired(self) -> tuple[bool, int]:
        """
        Check 90-day password rotation policy for local accounts only.
        Returns (is_expired: bool, days_left: int). AD accounts always return (False, 90).
        """
        if not self.is_local_account:
            return False, 90
        if self.must_change_password:
            return True, 0
        from .password_policy import is_password_expired
        return is_password_expired(self.password_changed_at, self.date_joined)

    @property
    def has_signature(self) -> bool:
        return bool(self.signature)

    def has_role(self, role_name: str) -> bool:
        """Return whether this user has a business role or is a Django superuser."""
        return self.is_superuser or self.groups.filter(name=role_name).exists()

    @property
    def role_names(self) -> list[str]:
        return list(self.groups.order_by("name").values_list("name", flat=True))

    def clean(self) -> None:
        super().clean()
        if self.supervisor_id and self.supervisor_id == self.pk:
            raise ValidationError({"supervisor": "ผู้ใช้ไม่สามารถเป็นหัวหน้าของตนเองได้"})
        for field_name in ("executive", "accounting_manager", "accountant", "alternate_payer"):
            if self.pk and getattr(self, f"{field_name}_id") == self.pk:
                raise ValidationError({field_name: "ผู้ใช้ไม่สามารถกำหนดตนเองในสายอนุมัติได้"})

    def claim_payer(self):
        """Use an explicitly configured substitute for an accountant's own claim."""
        from .roles import ACCOUNTANT
        payer = self.alternate_payer if self.alternate_payer_id and self.groups.filter(name=ACCOUNTANT).exists() else self.accountant
        if payer and payer.pk != self.pk and payer.is_active:
            if payer.pk != self.alternate_payer_id or payer.groups.filter(name=ACCOUNTANT).exists():
                return payer
        return None
