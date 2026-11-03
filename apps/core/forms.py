from django import forms
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
    class Meta:
        model = SystemSettings
        fields = ("company_name", "head_office_document_prefix", "branch_document_prefix", "executive_approval_threshold", "cfo", "max_attachment_count", "max_attachment_size_mb", "daily_reminder_time", "log_retention_days", "require_signature_on_first_login", "claim_print_enabled", "auto_receive_enabled", "auto_receive_after_days", "petty_receive_after_days", "telegram_enabled", "ad_gateway_url", "ad_app_id", "ad_secret_key", "ad_origin_ip", "ad_enabled", "ciam_enabled", "maintenance_mode", "maintenance_message")
        labels = {"company_name": "ชื่อบริษัทในเอกสาร", "document_number_prefix": "Prefix เลขที่เอกสาร", "executive_approval_threshold": "ยอดเบิกที่ต้องให้ CFO อนุมัติ (บาท)", "cfo": "CFO ผู้รับทราบและอนุมัติ", "max_attachment_count": "จำนวนไฟล์แนบสูงสุดต่อคำขอ", "max_attachment_size_mb": "ขนาดไฟล์แนบสูงสุดต่อไฟล์ (MB)", "daily_reminder_time": "เวลาแจ้งเตือนงานค้างรายวัน", "log_retention_days": "ระยะเวลาเก็บ Log (วัน)", "require_signature_on_first_login": "บังคับเพิ่มลายเซ็นก่อนใช้งานครั้งแรก", "claim_print_enabled": "เปิดใช้งานเอกสารสำหรับพิมพ์", "auto_receive_enabled": "ถือว่ารับเงินอัตโนมัติเมื่อครบกำหนด", "auto_receive_after_days": "เบิกยืมเงิน: รอกดยืนยันรับเงิน (วัน)", "petty_receive_after_days": "เบิกเงินสดย่อย: รอกดยืนยันรับเงิน (วัน)", "ciam_enabled": "เปิดใช้งาน CIAM", "maintenance_mode": "เปิดโหมดปรับปรุงระบบ", "maintenance_message": "ข้อความแจ้งผู้ใช้งาน"}
        widgets = {"ad_secret_key": forms.PasswordInput(render_value=False), "daily_reminder_time": forms.TimeInput(format="%H:%M", attrs={"type": "time"}), "auto_receive_after_days": forms.NumberInput(attrs={"min": "1", "max": "365", "step": "1"}), "petty_receive_after_days": forms.NumberInput(attrs={"min": "1", "max": "365", "step": "1"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts.models import User
        self.fields["cfo"].queryset = User.objects.filter(is_active=True).order_by("first_name", "username")
        self.fields["cfo"].help_text = "เลือก CFO 1 คน ผู้รับทราบทุกคำขอ และร่วมอนุมัติกรณียอดเกินเกณฑ์หรือผู้ขอเป็น Manager ขึ้นไป"
        self.fields["ad_secret_key"].required = False
        self.fields["ad_secret_key"].help_text = "เว้นว่างเพื่อใช้ Secret เดิม"
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

    def save(self, commit=True):
        if not self.cleaned_data.get("ad_secret_key") and self.instance.pk:
            self.instance.ad_secret_key = type(self.instance).objects.get(pk=self.instance.pk).ad_secret_key
        return super().save(commit)
