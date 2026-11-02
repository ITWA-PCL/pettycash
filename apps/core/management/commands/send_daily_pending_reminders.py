from collections import Counter, defaultdict
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError
from django.utils import timezone

from apps.claims.models import ClaimRequest
from apps.core.audit import log_event
from apps.core.models import DailyReminderRun
from apps.core.system_settings import get_system_settings
from apps.core.telegram import send_telegram_group_notification, send_telegram_notification


PENDING_STATUSES = (
    ClaimRequest.Status.DRAFT,
    ClaimRequest.Status.RETURNED,
    ClaimRequest.Status.PENDING_SUPERVISOR,
    ClaimRequest.Status.PENDING_EXECUTIVE,
    ClaimRequest.Status.PENDING_ACCOUNTING,
    ClaimRequest.Status.PENDING_PAYMENT,
    ClaimRequest.Status.PAID,
    ClaimRequest.Status.PENDING_CLEARANCE,
    ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW,
    ClaimRequest.Status.PENDING_EXCESS_APPROVAL,
)


def responsible_user(claim):
    if claim.status in (ClaimRequest.Status.DRAFT, ClaimRequest.Status.RETURNED, ClaimRequest.Status.PAID):
        return claim.requester
    if claim.status == ClaimRequest.Status.PENDING_SUPERVISOR:
        return claim.supervisor
    if claim.status == ClaimRequest.Status.PENDING_EXECUTIVE:
        return claim.executive
    if claim.status == ClaimRequest.Status.PENDING_ACCOUNTING and claim.accounting_approved_at:
        return claim.cfo if claim.requires_cfo_approval and not claim.cfo_approved_at else None
    if claim.status in (ClaimRequest.Status.PENDING_ACCOUNTING, ClaimRequest.Status.PENDING_EXCESS_APPROVAL):
        return claim.current_approver()
    if claim.status == ClaimRequest.Status.PENDING_CLEARANCE:
        return claim.requester
    if claim.status in (ClaimRequest.Status.PENDING_PAYMENT, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW):
        return claim.accountant
    return None


class Command(BaseCommand):
    help = "Send each user a daily Telegram reminder for outstanding work and a summary to the overview group."

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="Send after the configured morning time, once per date.")
        parser.add_argument("--force", action="store_true", help="Send now, ignoring today's scheduled-run marker.")

    def handle(self, *args, **options):
        if options["if_due"] and options["force"]:
            raise CommandError("Use either --if-due or --force, not both.")
        scheduled_time = get_system_settings().daily_reminder_time

        today = timezone.localdate()
        if options["if_due"]:
            if timezone.localtime().time() < scheduled_time:
                self.stdout.write("Daily reminder is not due yet.")
                return
            if DailyReminderRun.objects.filter(run_date=today).exists():
                self.stdout.write("Daily reminder was already sent today.")
                return

        claims = list(
            ClaimRequest.objects.filter(status__in=PENDING_STATUSES)
            .select_related("requester", "supervisor", "executive", "accounting_manager", "accountant", "cfo")
            .order_by("status", "updated_at")
        )
        by_recipient, status_counts = defaultdict(list), Counter()
        for claim in claims:
            status_counts[claim.workflow_status_display] += 1
            recipients = [responsible_user(claim)]
            if claim.status == ClaimRequest.Status.PENDING_ACCOUNTING and claim.accounting_approved_at and claim.requires_cfo_approval and not claim.cfo_approved_at:
                recipients.append(claim.cfo)
            for recipient in {user.pk: user for user in recipients if user}.values():
                by_recipient[recipient.pk].append((recipient, claim))

        direct_count = 0
        for recipient_claims in by_recipient.values():
            recipient = recipient_claims[0][0]
            if not (recipient.telegram_notifications_enabled and recipient.telegram_user_id):
                continue
            visible_claims = recipient_claims[:10]
            lines = [f"• {claim.document_number or f'แบบร่าง #{claim.pk}'} — {claim.workflow_status_display}" for _, claim in visible_claims]
            if len(recipient_claims) > len(visible_claims):
                lines.append(f"• และอีก {len(recipient_claims) - len(visible_claims)} รายการ")
            message = (
                f"สวัสดี {recipient.get_full_name() or recipient.username}\n"
                f"เช้านี้คุณมีงานค้างที่ต้องดำเนินการ {len(recipient_claims)} รายการ\n\n" + "\n".join(lines)
            )
            if send_telegram_notification(recipient, "แจ้งเตือนงานค้างประจำวัน", message, "/"):
                direct_count += 1

        group_sent = False
        if claims:
            summary_lines = [f'• {label}: {count} รายการ' for label, count in status_counts.items()]
            summary = f"วันที่ {today.strftime('%d/%m/%Y')}\nงานค้างทั้งหมด {len(claims)} รายการ\n\n" + "\n".join(summary_lines)
            group_sent = send_telegram_group_notification("สรุปงานค้างประจำวัน", summary)

        if options["if_due"]:
            try:
                DailyReminderRun.objects.create(run_date=today)
            except IntegrityError:
                self.stdout.write("Daily reminder was already recorded by another scheduler.")
                return
        log_event(None, "DAILY_PENDING_REMINDER", str(today), f"ค้าง {len(claims)} รายการ; ส่งรายบุคคล {direct_count} คน; Group {'สำเร็จ' if group_sent else 'ไม่ได้ส่ง'}")
        self.stdout.write(self.style.SUCCESS(f"Outstanding: {len(claims)}; direct recipients: {direct_count}; group sent: {group_sent}."))
