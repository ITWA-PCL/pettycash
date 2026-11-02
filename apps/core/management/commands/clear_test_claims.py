from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.claims.models import ClaimAttachment, ClaimPayment, ClaimRequest
from apps.core.audit import log_event
from apps.core.models import Notification


class Command(BaseCommand):
    help = "Delete only the explicitly listed test claims for one requester, including their notifications and files."

    def add_arguments(self, parser):
        parser.add_argument("--ids", nargs="+", type=int, required=True)
        parser.add_argument("--requester", required=True)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--expected-count", type=int, required=True)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        ids = set(options["ids"])
        expected = options["expected_count"]
        if not options["confirm"] or expected < 1 or len(ids) != expected:
            raise CommandError("ต้องระบุ --confirm และจำนวน --ids ให้ตรงกับ --expected-count")
        actor = get_user_model().objects.filter(username=options["actor"], is_active=True, is_superuser=True).first()
        if actor is None:
            raise CommandError("--actor ต้องเป็น Admin ที่เปิดใช้งาน")
        with transaction.atomic():
            claims = ClaimRequest.objects.select_for_update().filter(pk__in=ids, requester__username=options["requester"])
            actual_ids = set(claims.values_list("pk", flat=True))
            if actual_ids != ids:
                raise CommandError(f"รายการไม่ตรง: พบ {sorted(actual_ids)} จากที่ระบุ {sorted(ids)}; ยังไม่ลบข้อมูล")
            paths = list(ClaimAttachment.objects.filter(claim_request_id__in=ids).values_list("file", flat=True))
            paths += list(ClaimPayment.objects.filter(claim_request_id__in=ids).values_list("slip", flat=True))
            links = [f"/claims/{claim_id}/" for claim_id in ids]
            notifications, _ = Notification.objects.filter(link__in=links).delete()
            claims.delete()
            log_event(actor, "CLEAR_TEST_CLAIMS", options["requester"], f"ลบคำขอทดสอบ {expected} รายการ: {', '.join(map(str, sorted(ids)))}; แจ้งเตือน {notifications} รายการ")
        removed_files = 0
        for path in set(filter(None, paths)):
            if any(path.startswith(f"{folder}/{claim_id}/") for folder in ("claim-attachments", "payment-slips") for claim_id in ids):
                default_storage.delete(path)
                removed_files += 1
        self.stdout.write(self.style.SUCCESS(f"ลบคำขอทดสอบ {expected} รายการ, แจ้งเตือน {notifications} รายการ, ไฟล์ {removed_files} ไฟล์"))
