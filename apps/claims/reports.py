"""Readable, non-duplicating Excel reports for the filtered claims list."""
from decimal import Decimal

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .models import ClaimPayment, ClaimRequest


def person(user):
    return (user.get_full_name() or user.username) if user else '-'


def append_text_safe(sheet, values):
    sheet.append(values)
    # User-entered text must never become an Excel formula.
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            cell.data_type = 's'


def build_claim_report(claims, filters):
    book = Workbook()
    book.remove(book.active)
    now = timezone.localtime()
    filter_text = ' | '.join(str(value) for value in (
        filters.get('search_query'), filters.get('selected_status_label'),
        filters.get('selected_status_group_label'), filters.get('selected_queue_label'),
        filters.get('work_location'),
        f"วันที่ขอ {filters.get('date_from') or 'ไม่จำกัด'} ถึง {filters.get('date_to') or 'ไม่จำกัด'}",
    ) if value)
    specs = (
        ('สรุปคำขอ', 'ClaimsSummary',
         ('เลขที่เอกสาร', 'วันที่ขอ', 'ค่าใช้จ่ายของเดือน', 'ประเภท', 'ผู้ขอ', 'แผนกผู้ขอ', 'สถานที่', 'วัตถุประสงค์', 'สถานะ',
          'ยอดขอเบิก', 'ยอดใช้จริง', 'จ่ายแล้วรวม (ตามประวัติ)', 'จ่ายส่วนต่างแล้ว', 'คืนแล้ว (ปิดงาน)', 'ต้องคืน (ยังไม่ปิด)', 'ต้องจ่ายเพิ่ม', 'เจ้าหน้าที่บัญชี', 'อัปเดตล่าสุด', 'หัวหน้าผู้ขอ', 'ผู้จัดการบัญชี', 'CFO'),
         (20, 14, 22, 30, 26, 18, 20, 38, 28, 18, 18, 23, 20, 22, 22, 20, 26, 22, 26, 26, 26),
         '1 แถวต่อคำขอ • ยอดว่าง = ยังไม่ทราบ • จ่ายส่วนต่างรวมอยู่ในจ่ายแล้วรวม ห้ามบวกซ้ำ • เงินคืน/ต้องจ่ายเพิ่มคำนวณจากประวัติจ่ายที่มี'),
        ('รายการค่าใช้จ่าย', 'ExpenseItems',
         ('เลขที่เอกสาร', 'วันที่ขอ', 'ค่าใช้จ่ายของเดือน', 'ผู้ขอ', 'ลำดับ', 'รายการ', 'BU', 'แผนก Budget', 'Budget', 'ยอดขอรายการ', 'ยอดใช้จริงรายการ', 'ผู้อนุมัติ Budget', 'สถานะคำขอ'),
         (20, 14, 22, 26, 10, 42, 18, 20, 32, 20, 22, 26, 28),
         '1 แถวต่อรายการ • ไม่ใส่ยอดรวมคำขอซ้ำ • รวมยอดระดับคำขอให้ดูแผ่นสรุปคำขอ'),
        ('ประวัติจ่ายเงิน', 'PaymentHistory',
         ('เลขที่เอกสาร', 'วันที่ขอ', 'ผู้ขอ', 'รอบจ่าย', 'วิธีจ่าย', 'จำนวนเงิน', 'ผู้จ่าย', 'วันเวลาจ่าย', 'ชื่อสลิป'),
         (20, 14, 26, 26, 18, 20, 26, 22, 42),
         '1 แถวต่อการจ่ายจริง • แยกเงินต้น/ส่วนต่าง • ใบเก่าที่ไม่มีประวัติจ่ายไม่สร้างยอดจ่ายสมมุติ'),
    )
    for title, _, headers, widths, note in specs:
        sheet = book.create_sheet(title)
        for row, value in ((1, f'Petty Cash — {title}'),
                           (2, f'ส่งออก {now:%d/%m/%Y %H:%M} น. | {len(claims)} คำขอ | {filter_text}'), (3, note)):
            sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(headers))
            sheet.cell(row, 1, value)
        sheet.append([])
        append_text_safe(sheet, headers)
        for index, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(index)].width = width
    summary, items_sheet, payments_sheet = book.worksheets
    for claim in claims:
        payments = list(claim.payments.all())
        paid = sum((p.amount for p in payments), Decimal('0')) if payments else None
        extra = sum((p.amount for p in payments if p.kind == ClaimPayment.Kind.SUPPLEMENTAL), Decimal('0')) if payments else None
        # A partial legacy ledger cannot prove the total funding amount.
        complete_ledger = any(p.kind == ClaimPayment.Kind.INITIAL for p in payments)
        refund = due = None
        if complete_ledger and claim.actual_total is not None:
            refund = max(paid - claim.actual_total, Decimal('0'))
            due = max(claim.actual_total - paid, Decimal('0'))
        closed = claim.status in (ClaimRequest.Status.COMPLETED, ClaimRequest.Status.CANCELLED)
        append_text_safe(summary, (
            claim.document_number or 'แบบร่าง', claim.request_date, claim.expense_month_display,
            claim.get_request_type_display(), person(claim.requester), claim.requester.department,
            claim.location_display, claim.purpose, claim.workflow_status_display,
            claim.requested_total, claim.actual_total, paid, extra,
            refund if closed else None, refund if not closed else None, due,
            person(claim.accountant), timezone.localtime(claim.updated_at).replace(tzinfo=None),
            person(claim.supervisor), person(claim.accounting_manager), person(claim.cfo),
        ))
        for index, item in enumerate(claim.items.all(), 1):
            append_text_safe(items_sheet, (
                claim.document_number or 'แบบร่าง', claim.request_date, claim.expense_month_display,
                person(claim.requester), index, item.description, item.business_unit or claim.business_unit,
                item.department or claim.department, item.budget or claim.budget,
                item.requested_amount, item.actual_amount, person(item.budget_approver), claim.workflow_status_display,
            ))
        for payment in payments:
            append_text_safe(payments_sheet, (
                claim.document_number or 'แบบร่าง', claim.request_date, person(claim.requester),
                payment.get_kind_display(), payment.get_method_display(), payment.amount,
                person(payment.paid_by), timezone.localtime(payment.created_at).replace(tzinfo=None),
                payment.slip_original_name or ('มีสลิป' if payment.slip else '-'),
            ))
    for sheet, (_, table_name, headers, _, _) in zip(book.worksheets, specs):
        last_data = sheet.max_row
        for row in sheet.iter_rows():
            for cell in row:
                cell.font = Font(name='Tahoma', size=11, color='16324F')
                cell.alignment = Alignment(vertical='top', wrap_text=True)
        sheet['A1'].font = Font(name='Tahoma', size=18, bold=True, color='155AA7')
        sheet.row_dimensions[1].height = 30
        sheet.row_dimensions[2].height = 32
        sheet.row_dimensions[3].height = 40
        sheet.row_dimensions[5].height = 34
        for cell in sheet[5]:
            cell.fill = PatternFill('solid', fgColor='155AA7')
            cell.font = Font(name='Tahoma', size=11, color='FFFFFF', bold=True)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        for row in range(6, last_data + 1):
            sheet.row_dimensions[row].height = 42
            sheet.cell(row, 2).number_format = 'dd/mm/yyyy'
            if row % 2 == 0:
                for cell in sheet[row]:
                    cell.fill = PatternFill('solid', fgColor='EDF4FC')
        money_columns = {'สรุปคำขอ': range(10, 17), 'รายการค่าใช้จ่าย': (10, 11), 'ประวัติจ่ายเงิน': (6,)}[sheet.title]
        time_column = {'สรุปคำขอ': 18, 'ประวัติจ่ายเงิน': 8}.get(sheet.title)
        for row in range(6, last_data + 1):
            for column in money_columns:
                sheet.cell(row, column).number_format = '#,##0.00'
                sheet.cell(row, column).alignment = Alignment(horizontal='right', vertical='top')
            if time_column:
                sheet.cell(row, time_column).number_format = 'dd/mm/yyyy hh:mm'
        ref = f'A5:{get_column_letter(len(headers))}{last_data}'
        sheet.auto_filter.ref = ref
        if last_data >= 6:
            # Use one worksheet AutoFilter, not a second structured-table filter
            # over the same range. Keep banding as normal cell formatting.
            total_row = last_data + 2
            sheet.cell(total_row, 1, 'รวมแถวที่แสดง (เฉพาะยอดที่มีข้อมูล)')
            for column in money_columns:
                letter = get_column_letter(column)
                sheet.cell(total_row, column, f'=SUBTOTAL(109,{letter}6:{letter}{last_data})').number_format = '#,##0.00'
                sheet.cell(total_row, column).font = Font(name='Tahoma', bold=True, color='155AA7')
            sheet.row_dimensions[total_row].height = 36
        sheet.freeze_panes = 'A6'
        sheet.sheet_view.showGridLines = False
        sheet.print_title_rows = '1:5'
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = 'landscape'
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
    return book
