from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.claims.models import ClaimApproval, ClaimRequest
from apps.core.models import AdminAuditLog, Notification
from apps.core.system_settings import get_system_settings


@override_settings(TELEGRAM_BOT_TOKEN="")
class AutoConfirmReceiptsTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username="auto-receipt-requester", password="safe-test-password")
        self.requester.signature.name = "signatures/test/auto-receipt-requester.png"
        self.requester.save(update_fields=["signature"])
        self.accountant = User.objects.create_user(username="auto-receipt-accountant", password="safe-test-password")
        self.supervisor = User.objects.create_user(username="auto-receipt-supervisor", password="safe-test-password")
        self.manager = User.objects.create_user(username="auto-receipt-manager", password="safe-test-password")
        self.configuration = get_system_settings()

    def make_paid_claim(self, days_ago=4, request_type=ClaimRequest.RequestType.ADVANCE):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor,
            accounting_manager=self.manager, accountant=self.accountant, request_type=request_type,
            work_location="HQ", purpose="ทดสอบรับเงินอัตโนมัติ", status=ClaimRequest.Status.PAID,
        )
        payment = ClaimApproval.objects.create(
            claim_request=claim, actor=self.accountant, action=ClaimApproval.Action.PAID,
            status_before=ClaimRequest.Status.PENDING_PAYMENT, status_after=ClaimRequest.Status.PAID,
        )
        ClaimApproval.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        return claim

    @patch("apps.claims.views.send_telegram_notification")
    @patch("apps.claims.views.send_telegram_group_notification")
    def test_due_advance_moves_once_and_records_system_actor(self, group, direct):
        claim = self.make_paid_claim()

        call_command("auto_confirm_receipts", stdout=StringIO())
        claim.refresh_from_db()

        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_CLEARANCE)
        event = claim.approval_records.get(action=ClaimApproval.Action.AUTO_RECEIVED)
        self.assertIsNone(event.actor)
        self.assertEqual(event.status_before, ClaimRequest.Status.PAID)
        self.assertIn("ผู้ขอไม่ได้กดยืนยัน", event.comment)
        self.assertTrue(AdminAuditLog.objects.filter(action="AUTO_CONFIRM_RECEIPT", actor__isnull=True).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.requester, title__contains="อัตโนมัติ").exists())
        self.assertIn("ไม่ใช่การกดยืนยันของผู้ขอ", direct.call_args.args[2])
        group.assert_called_once()

        call_command("auto_confirm_receipts", stdout=StringIO())
        self.assertEqual(claim.approval_records.filter(action=ClaimApproval.Action.AUTO_RECEIVED).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.requester).count(), 1)
        self.client.force_login(self.requester)
        detail = self.client.get(reverse("claim_detail", args=[claim.pk]))
        self.assertContains(detail, "ระบบอัตโนมัติ")
        self.assertContains(detail, "ลงยอดใช้จริง")
        self.assertEqual(self.client.post(reverse("claim_receipt_confirm", args=[claim.pk])).status_code, 302)
        self.assertFalse(claim.approval_records.filter(action=ClaimApproval.Action.RECEIVED).exists())

        claim.status = ClaimRequest.Status.COMPLETED
        claim.save(update_fields=["status"])
        printed = self.client.get(reverse("claim_print", args=[claim.pk]))
        self.assertContains(printed, "ผู้ขอไม่ได้กดยืนยัน")
        self.assertNotContains(printed, 'alt="ลายเซ็นผู้รับเงิน"')

    def test_disabled_and_dry_run_do_not_change_request(self):
        claim = self.make_paid_claim()
        call_command("auto_confirm_receipts", "--dry-run", stdout=StringIO())
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)

        self.configuration.auto_receive_enabled = False
        self.configuration.save(update_fields=["auto_receive_enabled"])
        call_command("auto_confirm_receipts", stdout=StringIO())
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.assertFalse(claim.approval_records.filter(action=ClaimApproval.Action.AUTO_RECEIVED).exists())

    def test_configured_days_and_latest_payment_are_used(self):
        self.configuration.auto_receive_after_days = 3
        self.configuration.save(update_fields=["auto_receive_after_days"])
        not_due = self.make_paid_claim(days_ago=2)
        repaid = self.make_paid_claim(days_ago=5)
        latest_payment = ClaimApproval.objects.create(
            claim_request=repaid, actor=self.accountant, action=ClaimApproval.Action.PAID,
            status_before=ClaimRequest.Status.PENDING_PAYMENT, status_after=ClaimRequest.Status.PAID,
        )
        ClaimApproval.objects.filter(pk=latest_payment.pk).update(created_at=timezone.now() - timedelta(hours=12))
        no_payment_record = ClaimRequest.objects.create(
            requester=self.requester, request_type=ClaimRequest.RequestType.ADVANCE,
            work_location="HQ", purpose="ไม่มีเวลาจ่ายที่ตรวจสอบได้", status=ClaimRequest.Status.PAID,
        )
        petty_cash = self.make_paid_claim(days_ago=2, request_type=ClaimRequest.RequestType.PETTY_CASH)

        call_command("auto_confirm_receipts", stdout=StringIO())
        for claim in (not_due, repaid, no_payment_record, petty_cash):
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.PAID)

        self.configuration.auto_receive_after_days = 1
        self.configuration.save(update_fields=["auto_receive_after_days"])
        with patch("apps.core.management.commands.auto_confirm_receipts.notify"):
            call_command("auto_confirm_receipts", stdout=StringIO())
        not_due.refresh_from_db()
        self.assertEqual(not_due.status, ClaimRequest.Status.PENDING_CLEARANCE)
        for claim in (repaid, no_payment_record, petty_cash):
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.PAID)

    def test_manual_confirmation_prevents_later_automatic_action(self):
        claim = self.make_paid_claim()
        self.client.force_login(self.requester)
        self.assertEqual(self.client.post(reverse("claim_receipt_confirm", args=[claim.pk])).status_code, 302)
        call_command("auto_confirm_receipts", stdout=StringIO())
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_CLEARANCE)
        self.assertEqual(claim.approval_records.filter(action=ClaimApproval.Action.RECEIVED).count(), 1)
        self.assertFalse(claim.approval_records.filter(action=ClaimApproval.Action.AUTO_RECEIVED).exists())

    def test_petty_cash_auto_closes_after_three_days(self):
        not_due = self.make_paid_claim(days_ago=2, request_type=ClaimRequest.RequestType.PETTY_CASH)
        due = self.make_paid_claim(days_ago=4, request_type=ClaimRequest.RequestType.PETTY_CASH)
        with patch("apps.core.management.commands.auto_confirm_receipts.notify") as notify:
            call_command("auto_confirm_receipts", stdout=StringIO())
        not_due.refresh_from_db()
        due.refresh_from_db()
        self.assertEqual(not_due.status, ClaimRequest.Status.PAID)
        self.assertEqual(due.status, ClaimRequest.Status.COMPLETED)
        event = due.approval_records.get(action=ClaimApproval.Action.AUTO_RECEIVED)
        self.assertEqual(event.status_after, ClaimRequest.Status.COMPLETED)
        self.assertIn("ครบ 3 วัน", event.comment)
        self.assertIn("ปิดรายการ", notify.call_args.args[1])
        call_command("auto_confirm_receipts", stdout=StringIO())
        self.assertEqual(due.approval_records.filter(action=ClaimApproval.Action.AUTO_RECEIVED).count(), 1)

    def test_petty_cash_manual_receipt_closes_immediately(self):
        claim = self.make_paid_claim(days_ago=1, request_type=ClaimRequest.RequestType.PETTY_CASH)
        self.client.force_login(self.requester)
        self.assertEqual(self.client.post(reverse("claim_receipt_confirm", args=[claim.pk])).status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)
        self.assertTrue(claim.approval_records.filter(action=ClaimApproval.Action.RECEIVED).exists())
        call_command("auto_confirm_receipts", stdout=StringIO())
        self.assertFalse(claim.approval_records.filter(action=ClaimApproval.Action.AUTO_RECEIVED).exists())

    def test_advance_auto_moves_to_clearance_after_one_day(self):
        not_due = self.make_paid_claim(days_ago=0.5)
        due = self.make_paid_claim(days_ago=1.5)
        with patch("apps.core.management.commands.auto_confirm_receipts.notify"):
            call_command("auto_confirm_receipts", stdout=StringIO())
        not_due.refresh_from_db()
        due.refresh_from_db()
        self.assertEqual(not_due.status, ClaimRequest.Status.PAID)
        self.assertEqual(due.status, ClaimRequest.Status.PENDING_CLEARANCE)
