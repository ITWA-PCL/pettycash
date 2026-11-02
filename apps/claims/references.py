"""อ่านเอกสารที่บันทึกว่าใช้ประกอบยอดอนุมัติ ไม่เปิดสิทธิ์คำขออื่นทั่วไป"""
from .models import ClaimRequest


def exposure_snapshot(claim):
    if claim.excess_exposure_details is not None:
        return claim.excess_exposure_details
    return claim.approval_exposure_details


def can_view_references(user, claim):
    return user.is_superuser or user.pk in {
        claim.requester_id, claim.accounting_manager_id, claim.accountant_id, claim.cfo_id,
    } or user.has_role('ผู้ดูแลระบบ')


def reference_rows(claim):
    rows = [dict(row) for row in (exposure_snapshot(claim) or []) if not row.get('own') and row.get('id') != claim.pk]
    sources = {source.pk: source for source in ClaimRequest.objects.filter(
        requester_id=claim.requester_id, pk__in=[row.get('id') for row in rows],
    ).prefetch_related('items', 'attachments').order_by('pk')}
    for row in rows:
        row['source'] = sources.get(row.get('id'))
    return rows
