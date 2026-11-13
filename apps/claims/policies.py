"""กติกาเดือนค่าใช้จ่ายสำหรับการยื่นคำขอ"""
from datetime import timedelta

from django.utils import timezone


def current_date():
    return timezone.localdate()


def earliest_expense_month():
    today = current_date()
    month = today.replace(day=1)
    return (month - timedelta(days=1)).replace(day=1) if today.day <= 5 else month


def expense_month_error(expense_month):
    if expense_month and expense_month.replace(day=1) < earliest_expense_month():
        return 'เดือนค่าใช้จ่ายนี้พ้นกำหนดแล้ว สามารถเบิกย้อนหลังได้ถึงวันที่ 5 ของเดือนถัดไปเท่านั้น กรุณาเลือกเดือนค่าใช้จ่ายใหม่'
    return ''
