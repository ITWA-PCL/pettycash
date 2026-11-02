"""รายละเอียดประกอบยอดสะสมใน Telegram ไม่เปลี่ยนยอดหรือกฎอนุมัติ"""
from decimal import Decimal

from django.db.models import Min, Q

from .models import ClaimApproval, ClaimRequest


def daily_approval_breakdown(claim, snapshot, *, include_actual=False):
    saved = claim.excess_exposure_details if include_actual else claim.approval_exposure_details
    if saved is not None:
        lines = ['รายละเอียดที่ใช้อนุมัติ (รวมค้างจ่ายข้ามวัน บันทึก ณ ยื่น):']
        total = Decimal('0')
        for row in saved:
            amount = Decimal(row['amount'])
            total += amount
            label = f"ใบนี้ {row['document']}" if row.get('own') else row['document']
            lines.append(f"• {label}: {amount:,.2f} บาท ({row['date']} · {row['basis']})")
        lines.append(f'รวมตามรายละเอียดที่บันทึก: {total:,.2f} บาท')
        return '\n'.join(lines) + '\n'
    others = ClaimRequest.objects.filter(
        requester_id=claim.requester_id, request_date=claim.request_date,
    ).exclude(pk=claim.pk).exclude(status__in=(
        ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED,
        ClaimRequest.Status.REJECTED, ClaimRequest.Status.CANCELLED,
    ))
    boundary = claim.approval_records.filter(action__in=(
        ClaimApproval.Action.SUBMITTED, ClaimApproval.Action.EXCESS_SUBMITTED,
        ClaimApproval.Action.AMOUNT_CORRECTED, ClaimApproval.Action.ACTUAL_CORRECTED,
    )).order_by('-pk').first()
    if boundary:
        # ใบที่เพิ่งยื่นหลังรอบนี้ไม่ใช่ที่มาของยอดที่ใช้อนุมัติรอบนี้
        others = others.annotate(first_submission=Min('approval_records__created_at',
            filter=Q(approval_records__action=ClaimApproval.Action.SUBMITTED))).filter(
                Q(first_submission__lte=boundary.created_at) |
                Q(first_submission__isnull=True, created_at__lte=boundary.created_at))
    previous = list(others.order_by('document_number', 'pk'))
    own_amount = max(claim.requested_total, claim.actual_total or Decimal('0')) if include_actual else claim.requested_total
    if not previous and snapshot == own_amount:
        return ''
    lines = ['รายละเอียดใบก่อนในวันเดียวกัน (ข้อมูลล่าสุด):']
    total = Decimal('0')
    for other in previous:
        if other.status == ClaimRequest.Status.COMPLETED and other.actual_total is not None:
            amount, basis = other.actual_total, 'ใช้จริง ปิดแล้ว'
        else:
            amount = max(other.requested_total, other.actual_total or Decimal('0'))
            basis = 'รวมยอดใช้เกิน' if amount > other.requested_total else ('ยอดขอ ไม่มียอดจริงเดิม' if other.status == ClaimRequest.Status.COMPLETED else 'ยอดขอ ยังไม่ปิด')
        total += amount
        lines.append(f'• {other.document_number or f"คำขอ #{other.pk}"}: {amount:,.2f} บาท ({basis})')
    if not previous:
        lines.append('• ไม่พบใบก่อนที่นับได้ตามข้อมูลปัจจุบัน')
    label = 'ยอดใช้จริงรวม' if include_actual else 'ยอดขอ'
    lines.append(f'• ใบนี้ {claim.document_number or f"คำขอ #{claim.pk}"}: {own_amount:,.2f} บาท ({label})')
    total += own_amount
    lines.append(f'รวมตามรายละเอียดล่าสุด: {total:,.2f} บาท')
    if total != snapshot:
        lines.append(f'หมายเหตุ: ข้อมูลใบก่อนอาจเปลี่ยนหลังยื่น ยอดสะสมที่ใช้อนุมัติรอบนี้ยังเป็น {snapshot:,.2f} บาท ไม่ได้เปลี่ยนย้อนหลัง')
    return '\n'.join(lines) + '\n'


def telegram_message_parts(message, limit=2800):
    """แบ่งข้อความยาวตามบรรทัด ไม่ตัดรายการทิ้ง และเผื่อหัว/ลิงก์ของบอต"""
    parts, current = [], ''
    for line in message.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            parts.append(current)
            current = ''
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        current += line
    if current:
        parts.append(current)
    return parts or ['']
