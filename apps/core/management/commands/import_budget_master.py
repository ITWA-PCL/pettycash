from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from openpyxl import load_workbook

from apps.core.audit import log_event
from apps.core.models import Budget, BusinessUnit
from apps.core.system_settings import get_system_settings


def load_master_rows(path, kind=None):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        def rows(title, headers):
            if title not in workbook:
                raise CommandError(f'ไฟล์ต้องมีชีต {title}')
            sheet = workbook[title]
            if tuple(next(sheet.values, ())[:len(headers)]) != headers:
                raise CommandError(f'หัวตาราง {title} ไม่ตรงรูปแบบไฟล์')
            return sheet.iter_rows(min_row=2, values_only=True)

        units = []
        seen_codes = set()
        bu_rows = rows('Business unit', ('Distribution Rule', 'Distribution Rule Name')) if kind in (None, 'bu') else []
        for number, row in enumerate(bu_rows, start=2):
            if number > 10001:
                raise CommandError('แต่ละชีตต้องไม่เกิน 10,000 แถวข้อมูล')
            code, name = (str(value).strip() if value is not None else "" for value in row[:2])
            if not code and not name:
                continue
            if not code or not name or len(code) > 20 or len(name) > 150:
                raise CommandError(f"Business unit แถว {number}: รหัสหรือชื่อไม่ถูกต้อง")
            if code.casefold() in seen_codes:
                raise CommandError(f"Business unit แถว {number}: รหัส BU ซ้ำ {code}")
            seen_codes.add(code.casefold())
            units.append((code, name))

        budgets = []
        seen_budgets = set()
        budget_rows = rows('Budget', ('Expenses/Asset', 'Description', 'Department')) if kind in (None, 'budget') else []
        for number, row in enumerate(budget_rows, start=2):
            if number > 10001:
                raise CommandError('แต่ละชีตต้องไม่เกิน 10,000 แถวข้อมูล')
            category, name, department = (str(value).strip() if value is not None else "" for value in row[:3])
            if not category and not name and not department:
                continue
            if category not in Budget.Category.values or not name or not department or len(name) > 150 or len(department) > 150:
                raise CommandError(f"Budget แถว {number}: ประเภท ชื่อ หรือแผนกไม่ถูกต้อง")
            key = (department.casefold(), name.casefold())
            if key in seen_budgets:
                raise CommandError(f"Budget แถว {number}: Budget ซ้ำในแผนก {department}: {name}")
            seen_budgets.add(key)
            budgets.append((category, name, department))
        if (kind in (None, 'bu') and not units) or (kind in (None, 'budget') and not budgets):
            raise CommandError('ชีตที่นำเข้าต้องมีข้อมูลอย่างน้อย 1 รายการ')
        return units, budgets
    finally:
        workbook.close()


class Command(BaseCommand):
    help = "Import BU and Budget master data from the supplied workbook. --replace removes old master rows."

    def add_arguments(self, parser):
        parser.add_argument("workbook", type=Path)
        parser.add_argument("--owner", help="Username recorded as the owner of imported Budget rows")
        parser.add_argument("--replace", action="store_true", help="Replace all existing BU and Budget master rows")
        parser.add_argument("--dry-run", action="store_true", help="Validate and report counts without changing the database")

    def handle(self, *args, **options):
        path = options["workbook"]
        if not path.is_file():
            raise CommandError(f"ไม่พบไฟล์ {path}")
        units, budgets = load_master_rows(path)
        self.stdout.write(f"ตรวจไฟล์ผ่าน: BU {len(units)} รายการ, Budget {len(budgets)} รายการ")
        if options["dry_run"]:
            return
        if not options["replace"]:
            raise CommandError("การนำเข้าต้องระบุ --replace เพื่อยืนยันว่าจะแทนที่ข้อมูล master เดิม")
        if not options["owner"]:
            raise CommandError("กรุณาระบุ --owner สำหรับบันทึกผู้รับผิดชอบการนำเข้า")
        owner = get_user_model().objects.filter(username=options["owner"], is_active=True).first()
        if owner is None:
            raise CommandError("ไม่พบผู้ใช้ที่เปิดใช้งานตาม --owner")
        with transaction.atomic():
            configuration = get_system_settings()
            type(configuration).objects.select_for_update().get(pk=configuration.pk)
            old_budget_count = Budget.objects.count()
            old_bu_count = BusinessUnit.objects.count()
            Budget.objects.all().delete()
            BusinessUnit.objects.all().delete()
            BusinessUnit.objects.bulk_create([BusinessUnit(code=code, name=name) for code, name in units])
            Budget.objects.bulk_create([
                Budget(owner=owner, category=category, name=name, department=department)
                for category, name, department in budgets
            ])
            log_event(owner, "IMPORT_BUDGET_MASTER", path.name, f"แทนที่ BU {old_bu_count} → {len(units)} และ Budget {old_budget_count} → {len(budgets)} รายการ")
        self.stdout.write(self.style.SUCCESS(f"นำเข้าสำเร็จ: BU {len(units)} รายการ, Budget {len(budgets)} รายการ"))
