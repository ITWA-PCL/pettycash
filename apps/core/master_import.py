"""Import BU and Budget independently, using the original workbook columns."""
from io import BytesIO
import secrets
import time
from zipfile import ZipFile
import hashlib
import json

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.management import CommandError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils import timezone
from openpyxl import Workbook

from .audit import log_event
from .management.commands.import_budget_master import load_master_rows
from .models import Budget, BusinessUnit
from .system_settings import get_system_settings


SESSION_KEY = 'budget_master_preview'
IMPORT_TYPES = {
    'bu': {'label': 'BU', 'sheet': 'Business unit', 'description': 'รหัสและชื่อหน่วยธุรกิจ (Distribution Rule)',
           'headers': ['Distribution Rule', 'Distribution Rule Name'], 'columns': ['รหัส BU', 'ชื่อ BU'], 'icon': 'grid'},
    'budget': {'label': 'Budget', 'sheet': 'Budget', 'description': 'ประเภทค่าใช้จ่าย ชื่อ Budget และแผนกเจ้าของ',
               'headers': ['Expenses/Asset', 'Description', 'Department'], 'columns': ['ประเภท', 'ชื่อ Budget', 'แผนก'], 'icon': 'wallet'},
}


class MasterImportForm(forms.Form):
    workbook = forms.FileField(label='ไฟล์ Excel', widget=forms.FileInput(attrs={'accept': '.xlsx'}))
    mode = forms.ChoiceField(label='วิธีนำเข้า', choices=(
        ('merge', 'เพิ่ม / อัปเดตจากไฟล์ โดยเก็บรายการอื่นไว้'),
        ('replace', 'ใช้ไฟล์เป็นรายการหลัก'),
    ))

    def __init__(self, *args, kind='budget', **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind

    def clean_workbook(self):
        upload = self.cleaned_data['workbook']
        if not upload.name.lower().endswith('.xlsx') or upload.size > 5 * 1024 * 1024:
            raise forms.ValidationError('ใช้ไฟล์ .xlsx ขนาดไม่เกิน 5 MB')
        content = upload.read()
        try:
            with ZipFile(BytesIO(content)) as archive:
                if sum(row.file_size for row in archive.infolist()) > 32 * 1024 * 1024:
                    raise forms.ValidationError('ข้อมูลในไฟล์มีขนาดเกินกำหนด')
            self.rows = load_master_rows(BytesIO(content), kind=self.kind)
        except CommandError as error:
            raise forms.ValidationError(str(error)) from None
        except forms.ValidationError:
            raise
        except Exception:
            raise forms.ValidationError('อ่านไฟล์ Excel ไม่ได้ กรุณาใช้แม่แบบที่กำหนด') from None
        return upload


def plan_import(units, budgets, mode, owner, kind='both'):
    """Resolve natural keys and validate before making any changes."""
    if kind == 'bu':
        budgets = []
    elif kind == 'budget':
        units = []
    unit_map, budget_map = {}, {}
    for row in BusinessUnit.objects.all() if kind in ('bu', 'both') else []:
        key = row.code.casefold()
        if key in unit_map:
            raise ValueError('รหัส BU เดิมซ้ำกันเมื่อไม่แยกตัวพิมพ์ กรุณาแก้ไขก่อนนำเข้า')
        unit_map[key] = row
    for row in Budget.objects.all() if kind in ('budget', 'both') else []:
        key = (row.department.casefold(), row.name.casefold())
        if key in budget_map:
            raise ValueError('Budget เดิมซ้ำในแผนกเมื่อไม่แยกตัวพิมพ์ กรุณาแก้ไขก่อนนำเข้า')
        budget_map[key] = row
    summary = dict(bu_new=0, bu_updated=0, budget_new=0, budget_updated=0, bu_removed=0, budget_removed=0)
    if mode == 'replace':
        summary.update(bu_new=len(units), budget_new=len(budgets), bu_removed=len(unit_map), budget_removed=len(budget_map))
        return summary, [], []
    unit_changes, budget_changes = [], []
    for code, name in units:
        existing = unit_map.get(code.casefold())
        if existing is None:
            row = BusinessUnit(code=code, name=name)
            summary['bu_new'] += 1
        elif existing.name != name:
            row = existing
            row.name = name
            summary['bu_updated'] += 1
        else:
            continue
        row.full_clean(validate_unique=False, validate_constraints=False)
        unit_changes.append(row)
    for category, name, department in budgets:
        existing = budget_map.get((department.casefold(), name.casefold()))
        if existing is None:
            row = Budget(owner=owner, category=category, name=name, department=department)
            summary['budget_new'] += 1
        elif existing.category != category:
            row = existing
            row.category = category
            summary['budget_updated'] += 1
        else:
            continue
        row.full_clean(exclude=['owner'], validate_unique=False, validate_constraints=False)
        budget_changes.append(row)
    return summary, unit_changes, budget_changes


def master_state(kind='both'):
    rows = [list(BusinessUnit.objects.order_by('pk').values_list('pk', 'code', 'name')) if kind in ('bu', 'both') else [],
            list(Budget.objects.order_by('pk').values_list('pk', 'category', 'name', 'department', 'owner_id')) if kind in ('budget', 'both') else []]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def apply_import(units, budgets, mode, owner, *, token='', expected_state=None, kind='both'):
    if kind not in ('bu', 'budget', 'both') or mode not in ('merge', 'replace'):
        raise ValueError('ประเภทหรือวิธีนำเข้าไม่ถูกต้อง')
    if kind == 'bu':
        budgets = []
    elif kind == 'budget':
        units = []
    with transaction.atomic():
        configuration = get_system_settings()
        type(configuration).objects.select_for_update().get(pk=configuration.pk)
        if expected_state is not None and master_state(kind) != expected_state:
            raise ValueError('ข้อมูล master เปลี่ยนหลังตรวจไฟล์ กรุณาดูตัวอย่างใหม่')
        from .models import AdminAuditLog
        if token and AdminAuditLog.objects.filter(action='IMPORT_BUDGET_MASTER', target=f'upload:{token}').exists():
            raise ValueError('ไฟล์นี้นำเข้าไปแล้ว')
        summary, unit_changes, budget_changes = plan_import(units, budgets, mode, owner, kind)
        if mode == 'replace':
            if kind in ('bu', 'both'):
                BusinessUnit.objects.all().delete()
                BusinessUnit.objects.bulk_create([BusinessUnit(code=code, name=name) for code, name in units])
            if kind in ('budget', 'both'):
                Budget.objects.all().delete()
                Budget.objects.bulk_create([Budget(owner=owner, category=category, name=name, department=department) for category, name, department in budgets])
        else:
            now = timezone.now()
            for model, changes, fields in [(BusinessUnit, unit_changes, ['name', 'updated_at']),
                                          (Budget, budget_changes, ['category', 'updated_at'])]:
                created = [row for row in changes if row.pk is None]
                updated = [row for row in changes if row.pk is not None]
                for row in updated:
                    row.updated_at = now
                model.objects.bulk_create(created, batch_size=500)
                model.objects.bulk_update(updated, fields, batch_size=500)
        log_event(owner, 'IMPORT_BUDGET_MASTER', f'upload:{token}' if token else kind, f'{kind}/{mode}: BU {len(units)}, Budget {len(budgets)}; {summary}')
        return summary


@login_required
def budget_import(request):
    from .views import can_manage_budget_master
    if not can_manage_budget_master(request.user):
        return HttpResponseForbidden('คุณไม่มีสิทธิ์นำเข้า BU และ Budget')
    if request.method not in ('GET', 'POST'):
        return HttpResponse(status=405)
    kind = request.POST.get('kind', request.GET.get('kind', 'budget'))
    if kind not in IMPORT_TYPES:
        return HttpResponse('ประเภทการนำเข้าไม่ถูกต้อง', status=400)
    meta = IMPORT_TYPES[kind]
    session_key = SESSION_KEY + '_' + kind
    return_url = reverse_import(kind)
    preview = None
    form = MasterImportForm(kind=kind)
    if request.method == 'POST' and request.POST.get('action') == 'confirm':
        draft = request.session.get(session_key)
        valid = (draft and draft.get('kind') == kind and draft.get('user_id') == request.user.pk and time.time() - draft.get('created_at', 0) < 1800
                 and secrets.compare_digest(request.POST.get('token', ''), draft.get('token', '')))
        if not valid:
            messages.error(request, 'ตัวอย่างหมดอายุหรือไม่ถูกต้อง กรุณาเลือกไฟล์และตรวจใหม่')
            return redirect(return_url)
        try:
            apply_import(draft['units'], draft['budgets'], draft['mode'], request.user,
                         token=draft['token'], expected_state=draft['state'], kind=kind)
        except (ValueError, IntegrityError, forms.ValidationError):
            messages.error(request, 'นำเข้าไม่สำเร็จ ข้อมูลเดิมยังอยู่ กรุณาตรวจไฟล์ใหม่')
            return redirect(return_url)
        del request.session[session_key]
        messages.success(request, f"นำเข้า {meta['label']} สำเร็จ {len(draft['rows'])} รายการ")
        return redirect('budget_settings')
    if request.method == 'POST':
        # A failed preview must never leave a previous file ready for confirmation.
        request.session.pop(session_key, None)
        form = MasterImportForm(request.POST, request.FILES, kind=kind)
        if form.is_valid():
            units, budgets = form.rows
            mode = form.cleaned_data['mode']
            try:
                with transaction.atomic():
                    configuration = get_system_settings()
                    type(configuration).objects.select_for_update().get(pk=configuration.pk)
                    summary, _, _ = plan_import(units, budgets, mode, request.user, kind)
                    state = master_state(kind)
            except (ValueError, forms.ValidationError) as error:
                form.add_error(None, str(error))
            else:
                selected = units if kind == 'bu' else budgets
                preview = {'units': units, 'budgets': budgets, 'kind': kind, 'rows': selected, 'mode': mode, 'summary': summary,
                    'stats': {'new': summary[kind + '_new'], 'updated': summary[kind + '_updated'], 'removed': summary[kind + '_removed']},
                    'filename': form.cleaned_data['workbook'].name, 'token': secrets.token_urlsafe(24),
                    'user_id': request.user.pk, 'created_at': time.time(), 'state': state}
                request.session[session_key] = preview
    return render(request, 'core/budget_import.html', {'form': form, 'preview': preview, 'kind': kind, 'meta': meta})


def reverse_import(kind):
    from django.urls import reverse
    return reverse('budget_import') + '?kind=' + kind


@login_required
def budget_import_template(request):
    from .views import can_manage_budget_master
    if not can_manage_budget_master(request.user):
        return HttpResponseForbidden('คุณไม่มีสิทธิ์นำเข้า BU และ Budget')
    kind = request.GET.get('kind', 'budget')
    if kind not in IMPORT_TYPES:
        return HttpResponse(status=400)
    workbook = Workbook()
    workbook.active.title = IMPORT_TYPES[kind]['sheet']
    workbook.active.append(IMPORT_TYPES[kind]['headers'])
    workbook.active.append(['IT', 'แผนกไอที'] if kind == 'bu' else ['Expense', 'ค่าใช้จ่ายสำนักงาน', 'IT'])
    for sheet in workbook:
        sheet.freeze_panes = 'A2'
        for letter in ('A', 'B', 'C'):
            sheet.column_dimensions[letter].width = 32
    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(output.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{kind}-template.xlsx"'
    return response
