from datetime import date
from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from apps.accounts.models import User
from apps.accounts.roles import ACCOUNTANT
from .models import ClaimPayment, ClaimRequest
from .reports import build_claim_report


class OrganizedReportTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username='report-requester')
        self.accountant = User.objects.create_user(username='report-accountant')
        self.accountant.signature.name = 'signatures/test/report.png'
        self.accountant.save()
        self.accountant.groups.add(Group.objects.get(name=ACCOUNTANT))
        self.claim = self.make_claim('BR-REPORT-01', '100', '80')
        for amount in ('40', '60'):
            self.claim.items.create(description='Expense', requested_amount=amount, actual_amount=Decimal(amount)*Decimal('.8'))

    def make_claim(self, document, requested, actual, **kwargs):
        fields = dict(document_number=document, requester=self.requester, accountant=self.accountant,
            request_date=date(2026, 10, 24), expense_month=date(2026, 11, 1),
            status=ClaimRequest.Status.COMPLETED, purpose='Report test', work_location='HQ',
            requested_total=requested, actual_total=actual)
        fields.update(kwargs)
        return ClaimRequest.objects.create(**fields)

    def payment(self, claim, amount, kind=ClaimPayment.Kind.INITIAL):
        return claim.payments.create(amount=amount, kind=kind, method=ClaimPayment.Method.CASH, paid_by=self.accountant)

    def book(self, claims=None):
        claims = claims if claims is not None else list(ClaimRequest.objects.order_by('document_number'))
        stream = BytesIO()
        build_claim_report(claims, {}).save(stream)
        return load_workbook(BytesIO(stream.getvalue()))

    def test_one_summary_row_per_claim_no_duplicated_totals_and_item_amounts_separate(self):
        self.payment(self.claim, '100')
        book = self.book()
        summary = book['สรุปคำขอ']
        self.assertEqual(summary['J6'].value, 100)
        self.assertEqual(summary['K6'].value, 80)
        self.assertEqual(summary['L6'].value, 100)
        self.assertEqual(summary['N6'].value, 20)
        self.assertEqual(summary['J8'].value, '=SUBTOTAL(109,J6:J6)')
        self.assertEqual(summary.auto_filter.ref, 'A5:V6')
        items = book['รายการค่าใช้จ่าย']
        self.assertEqual(items['J6'].value + items['J7'].value, 100)
        self.assertEqual(items['K6'].value + items['K7'].value, 80)
        self.assertEqual(items.auto_filter.ref, 'A5:M7')

    def test_supplemental_paid_not_added_twice_and_pending_refund_is_not_returned(self):
        extra = self.make_claim('BR-REPORT-02', '4000', '5500')
        self.payment(extra, '4000')
        self.payment(extra, '1500', ClaimPayment.Kind.SUPPLEMENTAL)
        self.claim.status = ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW
        self.claim.save(update_fields=['status'])
        self.payment(self.claim, '100')
        book = self.book()
        summary = book['สรุปคำขอ']
        self.assertIsNone(summary['N6'].value)
        self.assertEqual(summary['O6'].value, 20)
        self.assertEqual(summary['L7'].value, 5500)
        self.assertEqual(summary['M7'].value, 1500)
        self.assertEqual(summary['P7'].value, 0)
        self.assertEqual(book['ประวัติจ่ายเงิน'].auto_filter.ref, 'A5:I8')

    def test_unknown_legacy_values_stay_blank_zero_actual_and_partial_ledger_not_invented(self):
        self.claim.actual_total = None
        self.claim.save(update_fields=['actual_total'])
        legacy = self.make_claim('BR-REPORT-02', '100', '0')
        self.payment(legacy, '20', ClaimPayment.Kind.SUPPLEMENTAL)
        book = self.book()
        summary = book.active
        self.assertIsNone(summary['K6'].value)
        self.assertIsNone(summary['L6'].value)
        self.assertIsNone(summary['N6'].value)
        self.assertEqual(summary['K7'].value, 0)
        self.assertEqual(summary['L7'].value, 20)
        self.assertIsNone(summary['N7'].value)

    def test_styles_dates_freeze_totals_and_empty_export(self):
        self.payment(self.claim, '100')
        book = self.book()
        for sheet in book:
            self.assertEqual(sheet.freeze_panes, 'A6')
            self.assertTrue(sheet['A5'].font.bold)
            self.assertEqual(sheet['B6'].number_format, 'dd/mm/yyyy')
            self.assertEqual(sheet.page_setup.orientation, 'landscape')
        self.assertEqual(book.active['C6'].value, 'พฤศจิกายน 2569')
        self.assertEqual(book.active['K6'].number_format, '#,##0.00')
        empty = self.book([])
        for sheet in empty:
            self.assertEqual(sheet.max_row, 5)
            self.assertFalse(sheet.tables)

    def test_user_supplied_formula_text_is_literal(self):
        self.claim.purpose = '=HYPERLINK("https://example.invalid","click")'
        self.claim.save(update_fields=['purpose'])
        item = self.claim.items.first()
        item.description = '=1+1'
        item.save(update_fields=['description'])
        book = self.book()
        self.assertEqual(book.active['H6'].data_type, 's')
        self.assertEqual(book['รายการค่าใช้จ่าย']['F6'].data_type, 's')

    def test_xlsx_uses_single_filter_no_structured_table_parts_requiring_excel_repair(self):
        self.payment(self.claim, '100')
        output = BytesIO()
        build_claim_report(list(ClaimRequest.objects.all()), {}).save(output)
        ns = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with ZipFile(BytesIO(output.getvalue())) as archive:
            self.assertFalse(any(name.startswith('xl/tables/') for name in archive.namelist()))
            for index in range(1, 4):
                root = ElementTree.fromstring(archive.read(f'xl/worksheets/sheet{index}.xml'))
                self.assertEqual(len(root.findall('x:autoFilter', ns)), 1)
                self.assertIsNone(root.find('x:tableParts', ns))
        book = self.book()
        for sheet in book:
            self.assertFalse(sheet.tables)
            self.assertEqual(sheet['A6'].fill.fgColor.rgb, '00EDF4FC')

    def test_export_respects_search_and_visibility_across_all_sheets(self):
        hidden = self.make_claim('BR-HIDDEN-01', '700', '700', accountant=None)
        self.payment(hidden, '700')
        self.make_claim('BR-REPORT-02', '200', '200')
        self.payment(self.claim, '100')
        self.client.force_login(self.accountant)
        response = self.client.get(reverse('claim_export'), {'q': 'BR-REPORT-01', 'status': 'COMPLETED'})
        self.assertEqual(response.status_code, 200)
        book = load_workbook(BytesIO(response.content))
        for sheet in book:
            content = str(list(sheet.values))
            self.assertNotIn('BR-HIDDEN-01', content)
            self.assertNotIn('BR-REPORT-02', content)
            self.assertIn('BR-REPORT-01', content)
        self.assertIn('BR-REPORT-01', book.active['A2'].value)
