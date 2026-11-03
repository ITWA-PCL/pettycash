import base64
from io import BytesIO

from django import forms
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from PIL import Image

from apps.core.models import BusinessUnit
from .models import User
from .roles import ACCOUNTANT, ACCOUNTING_APPROVER, REQUESTER, ROLE_NAMES, SUPERVISOR_APPROVER


class ProfilePasswordForm(forms.Form):
    password = forms.CharField(
        label="รหัสผ่านใหม่",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "placeholder": "ตั้งรหัสผ่านใหม่"}),
        help_text="อย่างน้อย 8 ตัวอักษร",
    )

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_password(self):
        password = self.cleaned_data["password"]
        validate_password(password, self.user)
        return password

    def save(self):
        self.user.set_password(self.cleaned_data["password"])
        self.user.save(update_fields=["password"])
        return self.user


class SignatureSetupForm(forms.ModelForm):
    signature_upload = forms.ImageField(required=False)
    signature_data = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = User
        fields = ()

    def clean(self):
        cleaned_data = super().clean()
        upload = cleaned_data.get("signature_upload")
        drawing = cleaned_data.get("signature_data")
        if not upload and not drawing:
            raise ValidationError("กรุณาวาดลายเซ็นหรือเลือกรูปภาพลายเซ็น")
        if upload and upload.size > 2 * 1024 * 1024:
            self.add_error("signature_upload", "ไฟล์ลายเซ็นต้องมีขนาดไม่เกิน 2 MB")
        if drawing:
            try:
                header, encoded = drawing.split(",", 1)
                if header != "data:image/png;base64":
                    raise ValueError
                image_bytes = base64.b64decode(encoded, validate=True)
                if len(image_bytes) > 2 * 1024 * 1024:
                    raise ValueError
                image = Image.open(BytesIO(image_bytes))
                image.verify()
            except (ValueError, OSError, TypeError):
                self.add_error("signature_data", "ลายเซ็นที่วาดไม่ถูกต้อง กรุณาลองใหม่")
        return cleaned_data

    def save(self, commit=True):
        user = self.instance
        upload = self.cleaned_data.get("signature_upload")
        drawing = self.cleaned_data.get("signature_data")
        if upload:
            user.signature.save(upload.name, upload, save=False)
        elif drawing:
            _, encoded = drawing.split(",", 1)
            user.signature.save("signature.png", ContentFile(base64.b64decode(encoded)), save=False)
        user.signature_updated_at = timezone.now()
        user.onboarding_completed = True
        if commit:
            user.save(update_fields=["signature", "signature_updated_at", "onboarding_completed"])
        return user


class UserManagementForm(forms.ModelForm):
    business_unit = forms.ChoiceField(label="BU", required=False)
    password = forms.CharField(
        label="รหัสผ่านชั่วคราว",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="ผู้ใช้สามารถเปลี่ยนภายหลังได้",
    )

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "position_level", "business_unit", "department", "work_location", "supervisor", "accounting_manager", "accountant", "alternate_payer", "telegram_user_id", "telegram_notifications_enabled", "is_ad_auth", "groups", "is_active")
        labels = {
            "username": "ชื่อผู้ใช้", "first_name": "ชื่อ", "last_name": "นามสกุล", "email": "อีเมล",
            "business_unit": "BU", "department": "แผนก", "work_location": "สถานที่ทำงาน",
            "position_level": "ระดับตำแหน่ง",
            "supervisor": "ผู้อนุมัติ", "accounting_manager": "ผู้จัดการบัญชี", "accountant": "เจ้าหน้าที่บัญชี",
            "telegram_user_id": "Telegram User ID", "telegram_notifications_enabled": "เปิดรับการแจ้งเตือนผ่าน Telegram",
            "is_ad_auth": "ใช้ Username นี้สำหรับ Login ด้วย Active Directory",
            "groups": "Role", "is_active": "เปิดใช้งานบัญชี",
        }
        widgets = {"groups": forms.CheckboxSelectMultiple}

    def __init__(self, *args, creating=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.creating = creating
        bu_codes = list(BusinessUnit.objects.order_by("code").values_list("code", flat=True))
        if self.instance.pk and self.instance.business_unit and self.instance.business_unit not in bu_codes:
            bu_codes.append(self.instance.business_unit)
        self.fields["business_unit"].choices = [("", "เลือก BU (Distribution Rule)")] + [(code, code) for code in sorted(bu_codes)]
        self.fields["position_level"].required = False
        self.fields["groups"].queryset = Group.objects.filter(name__in=ROLE_NAMES).order_by("name")
        self.fields["work_location"].choices = ((User.WorkLocation.HEAD_OFFICE, "สำนักงานใหญ่"), (User.WorkLocation.BRANCH, "กาญจนบุรี"))
        active_users = User.objects.filter(is_active=True).order_by("first_name", "username")
        self.fields["supervisor"].queryset = active_users.filter(groups__name=SUPERVISOR_APPROVER).distinct()
        self.fields["accounting_manager"].queryset = active_users.filter(groups__name=ACCOUNTING_APPROVER).distinct()
        self.fields["accountant"].queryset = active_users.filter(groups__name=ACCOUNTANT).distinct()
        self.fields["alternate_payer"].queryset = active_users.filter(groups__name=ACCOUNTANT).distinct()
        if self.instance.pk:
            for field_name in ("supervisor", "accounting_manager", "accountant", "alternate_payer"):
                self.fields[field_name].queryset = self.fields[field_name].queryset.exclude(pk=self.instance.pk)
        if creating:
            self.fields["password"].help_text = "จำเป็นสำหรับบัญชีภายในระบบเท่านั้น หากเปิด AD Login สามารถเว้นว่างได้"
        else:
            self.fields["password"].help_text = "เว้นว่างไว้หากไม่ต้องการเปลี่ยนรหัสผ่าน"

    def save(self, commit=True):
        user = super().save(commit=False)
        user.position_level = self.cleaned_data.get("position_level") or User.PositionLevel.EMPLOYEE
        user.auth_source = User.AuthSource.AD if user.is_ad_auth else User.AuthSource.LOCAL
        password = self.cleaned_data.get("password")
        if password:
            user.set_password(password)
        elif self.creating and user.is_ad_auth:
            user.set_unusable_password()
        if commit:
            user.save()
            self.save_m2m()
        return user

    def clean(self):
        cleaned_data = super().clean()
        if self.creating and not cleaned_data.get("is_ad_auth") and not cleaned_data.get("password"):
            self.add_error("password", "กรุณากำหนดรหัสผ่านสำหรับบัญชีภายในระบบ")
        roles = cleaned_data.get("groups")
        if roles and REQUESTER in set(roles.values_list("name", flat=True)):
            for field_name in ("supervisor", "accounting_manager", "accountant"):
                if field_name == "accountant" and ACCOUNTANT in set(roles.values_list("name", flat=True)) and cleaned_data.get("alternate_payer"):
                    continue
                if not cleaned_data.get(field_name):
                    self.add_error(field_name, "กรุณากำหนดสายอนุมัติสำหรับผู้ขอเบิก")
        if cleaned_data.get("telegram_notifications_enabled") and not cleaned_data.get("telegram_user_id"):
            self.add_error("telegram_user_id", "กรุณาระบุ Telegram User ID ก่อนเปิดการแจ้งเตือน")
        return cleaned_data


class ProfileDetailsForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "telegram_user_id")
        labels = {
            "first_name": "ชื่อ",
            "last_name": "นามสกุล",
            "email": "อีเมล",
            "telegram_user_id": "Telegram User ID",
        }
        widgets = {
            "first_name": forms.TextInput(attrs={"placeholder": "ชื่อ"}),
            "last_name": forms.TextInput(attrs={"placeholder": "นามสกุล"}),
            "email": forms.EmailInput(attrs={"placeholder": "name@example.com"}),
            "telegram_user_id": forms.TextInput(attrs={"placeholder": "กรอกเมื่อทราบ Telegram User ID"}),
        }
