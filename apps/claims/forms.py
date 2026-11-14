from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory

from apps.accounts.models import User
from apps.accounts.roles import SUPERVISOR_APPROVER
from apps.core.models import Budget, BusinessUnit

from .models import ClaimItem, ClaimRequest
from .fields import ThaiMonthField
from .policies import earliest_expense_month, expense_month_error


class ClaimRequestForm(forms.ModelForm):
    expense_month = ThaiMonthField(
        label="ค่าใช้จ่ายของเดือน", required=True,
        help_text="เดือนปัจจุบันเลือกได้ตลอดเดือน เดือนก่อนหน้าเลือกได้ถึงวันที่ 5 ของเดือนถัดไป และเลือกเดือนล่วงหน้าได้",
    )
    class Meta:
        model = ClaimRequest
        fields = ("request_type", "request_date", "expense_month", "work_location", "supervisor", "purpose")
        widgets = {
            "request_date": forms.DateInput(format="%d/%m/%Y", attrs={"class": "date-display-input", "inputmode": "numeric", "autocomplete": "off", "placeholder": "dd/mm/yyyy", "pattern": "\\d{2}/\\d{2}/\\d{4}"}),
            "purpose": forms.Textarea(attrs={"rows": 3, "placeholder": "ระบุวัตถุประสงค์ของการเบิกเงิน"}),
            "budget": forms.TextInput(attrs={"placeholder": "ระบุงบประมาณ (ถ้ามี)"}),
        }
        labels = {"request_type": "ประเภทการเบิก", "supervisor": "ผู้อนุมัติ", "request_date": "วันที่เกิดรายการ"}

    def __init__(self, *args, requester, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["request_date"].input_formats = ("%d/%m/%Y", "%d%m%Y", "%Y-%m-%d")
        self.fields['expense_month'].widget.attrs['data-min-month'] = earliest_expense_month().strftime('%Y-%m')
        self.requester = requester
        self.fields["request_type"].choices = [("", "เลือกประเภทการเบิก")] + list(ClaimRequest.RequestType.choices)
        self.fields["request_type"].required = True
        self.fields["supervisor"].required = True
        self.fields["request_type"].help_text = "เบิกเงินสดย่อย: ออกเงินไปก่อนและต้องแนบเอกสาร · เบิกยืมเงิน: รับเงินก่อนแล้วลงยอดใช้จริงภายหลัง"
        self.order_fields(("request_type", "request_date", "expense_month", "work_location", "supervisor", "purpose"))
        self.fields["work_location"].choices = (
            (User.WorkLocation.HEAD_OFFICE, "สำนักงานใหญ่"),
            (User.WorkLocation.BRANCH, "กาญจนบุรี"),
        )
        eligible_supervisors = User.objects.filter(groups__name=SUPERVISOR_APPROVER, is_active=True).exclude(pk=requester.pk).distinct()
        self.fields["supervisor"].queryset = eligible_supervisors.order_by("first_name", "username")
        if not self.instance.pk:
            self.initial["request_type"] = ""
            # Use the approver configured in the user's profile as the default,
            # but deliberately leave the select enabled for an allowed override.
            if requester.supervisor_id:
                self.initial["supervisor"] = requester.supervisor_id
            self.initial["work_location"] = requester.work_location if requester.work_location != User.WorkLocation.OTHER else ''
        if not eligible_supervisors.exists():
            self.fields["supervisor"].help_text = "ยังไม่มีผู้ใช้ที่มีสิทธิ์หัวหน้าผู้ขอ กรุณาให้ผู้ดูแลระบบกำหนดสิทธิ์ก่อน"

    def clean_supervisor(self):
        supervisor = self.cleaned_data["supervisor"]
        if supervisor == self.requester:
            raise ValidationError("ไม่สามารถเลือกตนเองเป็นผู้อนุมัติได้")
        return supervisor

    def clean_expense_month(self):
        value = self.cleaned_data['expense_month']
        error = expense_month_error(value)
        if error:
            raise ValidationError(error)
        return value

    def clean(self):
        cleaned_data = super().clean()
        return cleaned_data

    def validate_approvers(self, formset):
        """Every request uses the assigned first approver, regardless of amount."""
        return not self.errors

    def save(self, commit=True):
        claim = super().save(commit=False)
        claim.supervisor = self.cleaned_data.get("supervisor")
        if not claim.pk:
            claim.requester = self.requester
            claim.business_unit = self.requester.business_unit
            claim.department = self.requester.department
            claim.accounting_manager = self.requester.accounting_manager
            claim.accountant = self.requester.claim_payer()
            from apps.core.system_settings import get_system_settings
            claim.cfo = get_system_settings().cfo
            claim.requester_position_level = self.requester.position_level
        if commit:
            claim.save()
        return claim


class ClaimItemForm(forms.ModelForm):
    business_unit = forms.ChoiceField(label="Business unit")
    department = forms.ChoiceField(label="แผนกเจ้าของ Budget")
    budget = forms.ChoiceField(label="Budget")
    budget_approver = forms.ModelChoiceField(queryset=User.objects.none(), required=False, label="ผู้อนุมัติ Budget")

    class Meta:
        model = ClaimItem
        fields = ("description", "business_unit", "department", "budget", "budget_approver", "requested_amount")
        widgets = {
            "description": forms.TextInput(attrs={"placeholder": "เช่น ซื้ออุปกรณ์สำนักงาน"}),
            "requested_amount": forms.NumberInput(attrs={"min": "0.01", "step": "0.01", "placeholder": "0.00"}),
        }

    def __init__(self, *args, requester=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.requester = requester
        for name in ("description", "business_unit", "department", "budget", "requested_amount"):
            self.fields[name].required = True
        bu_codes = list(BusinessUnit.objects.order_by("code").values_list("code", flat=True))
        if self.instance.pk and self.instance.business_unit and self.instance.business_unit not in bu_codes:
            bu_codes.append(self.instance.business_unit)
        self.fields["business_unit"].choices = [("", "เลือก BU (Distribution Rule)")] + [(code, code) for code in sorted(bu_codes)]
        budgets = list(Budget.objects.exclude(department="").order_by("department", "name"))
        departments = {budget.department for budget in budgets}
        departments.update(User.objects.filter(is_active=True).exclude(department="").values_list("department", flat=True))
        if self.instance.pk and self.instance.department:
            departments.add(self.instance.department)
        self.fields["department"].choices = [("", "เลือกแผนก")] + [(name, name) for name in sorted(departments)]
        names = {budget.name for budget in budgets}
        if self.instance.pk and self.instance.budget:
            names.add(self.instance.budget)
        self.fields["budget"].choices = [("", "เลือก Budget")] + [(name, name) for name in sorted(names)]
        self.fields["budget_approver"].queryset = User.objects.filter(
            is_active=True, position_level=User.PositionLevel.MANAGER,
        ).exclude(pk=requester.pk if requester else None).order_by("department", "first_name", "username")

    def clean(self):
        cleaned = super().clean()
        department = cleaned.get("department")
        budget_name = cleaned.get("budget")
        approver = cleaned.get("budget_approver")
        if department and budget_name and not Budget.objects.filter(department=department, name=budget_name).exists():
            if not (self.instance.pk and self.instance.department == department and self.instance.budget == budget_name):
                self.add_error("budget", "Budget นี้ไม่อยู่ในแผนกที่เลือก")
        if self.requester and department:
            if department == self.requester.department:
                cleaned["budget_approver"] = None
            elif not approver:
                self.add_error("budget_approver", "กรุณาเลือก Manager ของแผนกเจ้าของ Budget")
            elif approver.department != department:
                self.add_error("budget_approver", "ผู้อนุมัติ Budget ต้องเป็น Manager ของแผนกที่เลือก")
        return cleaned


ClaimItemFormSet = inlineformset_factory(
    ClaimRequest, ClaimItem, form=ClaimItemForm, extra=1, min_num=1, validate_min=True, can_delete=True
)
