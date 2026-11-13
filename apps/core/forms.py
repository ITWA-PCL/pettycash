from django import forms
from urllib.parse import urlsplit
from django.conf import settings
import re
from .models import Budget, BusinessUnit, SystemSettings


class BusinessUnitForm(forms.ModelForm):
    class Meta:
        model = BusinessUnit
        fields = ("code", "name")
        labels = {"code": "Distribution Rule", "name": "Distribution Rule Name"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs["id"] = "id_bu_name"

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        existing = BusinessUnit.objects.filter(code__iexact=code)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError("Distribution Rule นี้มีอยู่แล้ว")
        return code


class BudgetForm(forms.ModelForm):
    class Meta:
        model = Budget
        fields = ("department", "name", "category")
        labels = {"department": "แผนกเจ้าของ Budget", "name": "ชื่อ Budget", "category": "Expenses/Asset"}
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "เช่น ค่าใช้จ่ายสำนักงาน"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts.models import User
        departments = set(User.objects.filter(is_active=True).exclude(department="").values_list("department", flat=True))
        departments.update(Budget.objects.exclude(department="").values_list("department", flat=True))
        if self.instance.pk and self.instance.department:
            departments.add(self.instance.department)
        self.fields["department"].widget = forms.Select(choices=[("", "เลือกแผนก")] + [(name, name) for name in sorted(departments)])
        self.fields["department"].required = True


class SystemSettingsForm(forms.ModelForm):
    integration_names = ('public_base_url', 'ad_gateway_url', 'ad_app_id', 'ad_secret_key', 'ad_origin_ip',
                         'telegram_bot_token', 'telegram_group_chat_id', 'telegram_webhook_secret',
                         'ciam_base_url', 'ciam_client_id', 'ciam_client_secret', 'ciam_m2m_key', 'ciam_auto_provision_group')

    class Meta:
        model = SystemSettings
        fields = (
            "company_name", "head_office_document_prefix", "branch_document_prefix",
            "executive_approval_threshold", "cfo", "max_attachment_count", "max_attachment_size_mb",
            "daily_reminder_time", "log_retention_days", "require_signature_on_first_login",
            "claim_print_enabled", "auto_receive_enabled", "auto_receive_after_days",
            "petty_receive_after_days", "telegram_enabled", "ad_gateway_url", "ad_app_id",
            "ad_secret_key", "ad_origin_ip", "ad_enabled",
            "ciam_base_url", "ciam_client_id", "ciam_client_secret", "ciam_m2m_key", "ciam_sso_enabled",
            "ciam_break_glass_active", "ciam_auto_provision_group",
            "maintenance_mode", "maintenance_message",
        )
        labels = {
            "company_name": "ชื่อบริษัทในเอกสาร", "document_number_prefix": "Prefix เลขที่เอกสาร",
            "executive_approval_threshold": "ยอดเบิกที่ต้องให้ CFO อนุมัติ (บาท)", "cfo": "CFO ผู้รับทราบและอนุมัติ",
            "max_attachment_count": "จำนวนไฟล์แนบสูงสุดต่อคำขอ", "max_attachment_size_mb": "ขนาดไฟล์แนบสูงสุดต่อไฟล์ (MB)",
            "daily_reminder_time": "เวลาแจ้งเตือนงานค้างรายวัน", "log_retention_days": "ระยะเวลาเก็บ Log (วัน)",
            "require_signature_on_first_login": "บังคับเพิ่มลายเซ็นก่อนใช้งานครั้งแรก", "claim_print_enabled": "เปิดใช้งานเอกสารสำหรับพิมพ์",
            "auto_receive_enabled": "ถือว่ารับเงินอัตโนมัติเมื่อครบกำหนด", "auto_receive_after_days": "เบิกยืมเงิน: รอกดยืนยันรับเงิน (วัน)",
            "petty_receive_after_days": "เบิกเงินสดย่อย: รอกดยืนยันรับเงิน (วัน)",
            "ciam_base_url": "Central IAM Base URL", "ciam_client_id": "OIDC Client ID",
            "ciam_client_secret": "OIDC Client Secret", "ciam_m2m_key": "Secret Key (X-Management-API-Key)",
            "ciam_sso_enabled": "เปิดใช้งาน Central IAM Single Sign-On (SSO)",
            "ciam_break_glass_active": "เปิดโหมดฉุกเฉิน Break-Glass (บังคับรหัสผ่านตรง)",
            "ciam_auto_provision_group": "กลุ่มสิทธิ์เริ่มต้นสำหรับพนักงานใหม่ (Auto-Provision)",
            "maintenance_mode": "เปิดโหมดปรับปรุงระบบ", "maintenance_message": "ข้อความแจ้งผู้ใช้งาน",
        }
        widgets = {
            "ad_secret_key": forms.PasswordInput(render_value=False),
            "ciam_client_secret": forms.PasswordInput(render_value=False),
            "ciam_m2m_key": forms.PasswordInput(render_value=False),
            "daily_reminder_time": forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
            "auto_receive_after_days": forms.NumberInput(attrs={"min": "1", "max": "365", "step": "1"}),
            "petty_receive_after_days": forms.NumberInput(attrs={"min": "1", "max": "365", "step": "1"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.integration_names:
            if name not in self.fields:
                self.fields[name] = self._meta.model._meta.get_field(name).formfield()
        labels = {'public_base_url': 'URL เว็บที่ใช้งานจริง', 'ad_gateway_url': 'AD Gateway URL',
            'ad_app_id': 'AD App ID', 'ad_secret_key': 'AD Secret Key', 'ad_origin_ip': 'AD Origin IP',
            'telegram_bot_token': 'Telegram Bot Token', 'telegram_group_chat_id': 'Telegram Group Chat ID',
            'telegram_webhook_secret': 'Telegram Webhook Secret'}
        for name, label in labels.items():
            self.fields[name].label = label
        self.initial['public_base_url'] = self.instance.public_base_url or settings.PUBLIC_BASE_URL or 'https://pettycash.windowasia.com'
        for name, env_name in (('ad_gateway_url', 'AD_GATEWAY_URL'), ('ad_app_id', 'AD_APP_ID'), ('ad_origin_ip', 'AD_ORIGIN_IP')):
            self.initial[name] = getattr(self.instance, name) or getattr(settings, env_name, '')
        self.initial['telegram_group_chat_id'] = self.instance.telegram_group_chat_id if self.instance.telegram_bot_token else self.instance.telegram_group_chat_id or settings.TELEGRAM_GROUP_CHAT_ID
        self.fields['public_base_url'].required = True
        self.fields['public_base_url'].help_text = 'ใช้สร้างลิงก์ในข้อความ Telegram และที่อยู่รับปุ่มตอบกลับ'
        self.fields['telegram_group_chat_id'].help_text = 'เว้นว่างได้หากไม่ส่งข้อความเข้ากลุ่ม'
        for name, env_name in (('ad_secret_key', 'AD_SECRET_KEY'), ('telegram_bot_token', 'TELEGRAM_BOT_TOKEN'), ('telegram_webhook_secret', 'TELEGRAM_WEBHOOK_SECRET'), ('ciam_client_secret', 'CIAM_CLIENT_SECRET'), ('ciam_m2m_key', 'CIAM_M2M_KEY')):
            self.fields[name].required = False
            self.fields[name].widget = forms.PasswordInput(render_value=False, attrs={'autocomplete': 'new-password'})
            configured = bool(getattr(self.instance, name) or getattr(settings, env_name, ''))
            self.fields[name].help_text = ('ตั้งค่าไว้แล้ว · ' if configured else '') + 'เว้นว่างเพื่อเก็บค่าเดิม; ใส่ค่าใหม่เมื่อต้องการเปลี่ยน'
        from apps.accounts.models import User
        self.fields["cfo"].queryset = User.objects.filter(is_active=True).order_by("first_name", "username")
        self.fields["cfo"].help_text = "เลือก CFO 1 คน ผู้รับทราบทุกคำขอ และร่วมอนุมัติกรณียอดเกินเกณฑ์หรือผู้ขอเป็น Manager ขึ้นไป"
        self.fields["ad_secret_key"].required = False
        self.fields["daily_reminder_time"].help_text = "ระบบจะตรวจทุก 5 นาที และส่งหนึ่งครั้งต่อวันหลังเวลานี้"
        self.fields["log_retention_days"].help_text = "ลบเฉพาะประวัติการใช้งานและการแจ้งเตือนที่เก่ากว่ากำหนด ไม่กระทบคำขอและเอกสาร"
        self.fields["auto_receive_after_days"].help_text = "เริ่มนับจากเวลาที่บัญชีจ่ายเงินยืม; ครบกำหนดแล้วส่งต่อไปรอเคลียร์"
        self.fields["petty_receive_after_days"].help_text = "เริ่มนับจากเวลาที่บัญชีจ่ายเงินสดย่อย; ครบกำหนดแล้วปิดรายการ"
        for name, label in (("head_office_document_prefix", "รหัสเอกสารสำนักงานใหญ่"), ("branch_document_prefix", "รหัสเอกสารกาญจนบุรี")):
            self.fields[name].label = label
            self.fields[name].help_text = "ภาษาอังกฤษหรือตัวเลขไม่เกิน 12 ตัว มีผลเฉพาะเอกสารที่ออกเลขใหม่"
        self.order_fields(("company_name", "executive_approval_threshold", "head_office_document_prefix", "branch_document_prefix"))

    def clean_head_office_document_prefix(self):
        return self.cleaned_data["head_office_document_prefix"].upper()

    def clean_branch_document_prefix(self):
        return self.cleaned_data["branch_document_prefix"].upper()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("head_office_document_prefix") and cleaned.get("head_office_document_prefix") == cleaned.get("branch_document_prefix"):
            self.add_error("branch_document_prefix", "รหัสเอกสารกาญจนบุรีต้องต่างจากสำนักงานใหญ่")
        return cleaned

    @property
    def integration_fields(self):
        return [self[name] for name in self.integration_names]

    def clean_public_base_url(self):
        value = self.cleaned_data['public_base_url'].rstrip('/')
        parts = urlsplit(value)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path:
            raise forms.ValidationError('ระบุ URL หลักแบบ HTTPS เช่น https://pettycash.windowasia.com โดยไม่ใส่ path')
        return value

    def clean_telegram_bot_token(self):
        value = self.cleaned_data.get('telegram_bot_token', '').strip()
        if value and not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', value):
            raise forms.ValidationError('Bot Token ต้องเป็นค่าที่ได้จาก BotFather')
        return value

    def clean_telegram_group_chat_id(self):
        value = self.cleaned_data.get('telegram_group_chat_id', '').strip()
        if value and not re.fullmatch(r'-?[0-9]+', value):
            raise forms.ValidationError('Group Chat ID ต้องเป็นตัวเลข เช่น -1001234567890')
        return value

    def clean_telegram_webhook_secret(self):
        value = self.cleaned_data.get('telegram_webhook_secret', '').strip()
        if value and not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value):
            raise forms.ValidationError('Webhook Secret ใช้ A–Z, a–z, 0–9, _ หรือ - ไม่เกิน 256 ตัว')
        return value

    def save(self, commit=True):
        # These fields are added above to keep existing settings forms compatible.
        previous = type(self.instance).objects.get(pk=self.instance.pk) if self.instance.pk else None
        for name in self.integration_names:
            value = self.cleaned_data.get(name, '')
            if name in ('ad_secret_key', 'telegram_bot_token', 'telegram_webhook_secret', 'ciam_client_secret', 'ciam_m2m_key') and not value and previous:
                value = getattr(previous, name)
            setattr(self.instance, name, value)
        return super().save(commit)
