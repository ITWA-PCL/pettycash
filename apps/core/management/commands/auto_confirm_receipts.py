from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from apps.claims.models import ClaimApproval, ClaimRequest
from apps.claims.views import notify
from apps.core.audit import log_event
from apps.core.system_settings import get_system_settings


class Command(BaseCommand):
    help = "Confirm unacknowledged receipts after the deadline for each request type."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count due requests without changing them.")

    def handle(self, *args, **options):
        configuration = get_system_settings()
        if not configuration.auto_receive_enabled:
            self.stdout.write("Automatic receipt confirmation is disabled.")
            return

        now = timezone.now()
        advance_cutoff = now - timedelta(days=configuration.auto_receive_after_days)
        petty_cutoff = now - timedelta(days=configuration.petty_receive_after_days)
        supplemental_cutoff = now - timedelta(days=1)
        latest_payment = ClaimApproval.objects.filter(
            claim_request_id=OuterRef("pk"), action__in=(ClaimApproval.Action.PAID, ClaimApproval.Action.SUPPLEMENTAL_PAID),
        ).order_by("-created_at", "-pk")
        due_ids = list(
            ClaimRequest.objects.filter(
                status=ClaimRequest.Status.PAID,
            )
            .annotate(latest_payment_at=Subquery(latest_payment.values('created_at')[:1]), latest_payment_action=Subquery(latest_payment.values('action')[:1]))
            .filter(
                Q(latest_payment_action=ClaimApproval.Action.PAID, request_type=ClaimRequest.RequestType.ADVANCE, latest_payment_at__lte=advance_cutoff)
                | Q(latest_payment_action=ClaimApproval.Action.PAID, request_type=ClaimRequest.RequestType.PETTY_CASH, latest_payment_at__lte=petty_cutoff)
                | Q(latest_payment_action=ClaimApproval.Action.SUPPLEMENTAL_PAID, latest_payment_at__lte=supplemental_cutoff)
            )
            .values_list("pk", flat=True)
        )
        if options["dry_run"]:
            self.stdout.write(f"Due receipts: {len(due_ids)} (dry run; no changes).")
            return

        completed = 0
        for claim_id in due_ids:
            with transaction.atomic():
                claim = ClaimRequest.objects.select_for_update(of=('self',)).select_related("requester").get(pk=claim_id)
                payment = claim.latest_payment_event
                payment_at = payment.created_at if payment else None
                advance = claim.request_type == ClaimRequest.RequestType.ADVANCE
                supplemental = payment is not None and payment.action == ClaimApproval.Action.SUPPLEMENTAL_PAID
                days = claim.receipt_wait_days(configuration)
                cutoff = now - timedelta(days=days)
                closes = not advance or supplemental
                next_status = ClaimRequest.Status.COMPLETED if closes else ClaimRequest.Status.PENDING_CLEARANCE
                next_step = "ปิดรายการ" if closes else "ส่งไปรอเคลียร์"
                kind = 'ส่วนต่าง' if supplemental else ''
                if (
                    claim.status != ClaimRequest.Status.PAID
                    or claim.request_type not in (ClaimRequest.RequestType.ADVANCE, ClaimRequest.RequestType.PETTY_CASH)
                    or payment_at is None
                    or payment_at > cutoff
                ):
                    continue
                claim.status = next_status
                claim.save(update_fields=["status", "updated_at"])
                ClaimApproval.objects.create(
                    claim_request=claim, actor=None, action=ClaimApproval.Action.AUTO_RECEIVED,
                    status_before=ClaimRequest.Status.PAID,
                    status_after=next_status,
                    comment=f"ครบ {days} วันหลังบัญชีบันทึกจ่ายเงิน{kind} {claim.receipt_amount:,.2f} บาท; ผู้ขอไม่ได้กดยืนยันรับเงิน ระบบ{next_step}อัตโนมัติ",
                )
                log_event(
                    None, "AUTO_CONFIRM_RECEIPT", claim.document_number or f"คำขอ #{claim.pk}",
                    f"ครบ {days} วันหลังจ่ายเงิน{kind} {claim.receipt_amount:,.2f} บาท; ระบบ{next_step}",
                )
            notify(
                claim.requester, f"ระบบถือว่ารับเงิน{kind}อัตโนมัติและปิดรายการแล้ว" if closes else "ระบบถือว่ารับเงินอัตโนมัติ กรุณาลงยอดใช้จริง",
                f"คำขอ {claim.document_number or f'#{claim.pk}'} ครบ {days} วันหลังบัญชีบันทึกจ่ายเงิน{kind} {claim.receipt_amount:,.2f} บาท "
                f"ระบบจึง{next_step}อัตโนมัติ (ไม่ใช่การกดยืนยันของผู้ขอ) "
                "หากยังไม่ได้รับเงินจริง กรุณาติดต่อเจ้าหน้าที่บัญชี",
                claim,
            )
            completed += 1

        self.stdout.write(self.style.SUCCESS(f"Automatically confirmed receipts: {completed}."))
