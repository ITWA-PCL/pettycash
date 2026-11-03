"""กติกาวันที่เกิดรายการสำหรับการยื่นคำขอ"""
from datetime import timedelta

from django.utils import timezone


def current_date():
    return timezone.localdate()


def backdate_error(claim, request_date):
    if request_date >= current_date() - timedelta(days=5):
        return ''
    if (claim.pk and claim.status == claim.Status.RETURNED and
            claim.accepted_request_date == request_date):
        return ''
    return 'เบิกย้อนหลังได้ไม่เกิน 5 วันนับจากวันที่เกิดรายการถึงวันที่ยื่นคำขอ'
