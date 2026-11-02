from datetime import date
import json
from io import BytesIO
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from apps.accounts.models import User
from apps.accounts.roles import ACCOUNTANT, EXECUTIVE_APPROVER, SUPER_ADMIN, SUPERVISOR_APPROVER
from apps.core.models import AdminAuditLog, Budget, BusinessUnit, Notification
from apps.core.system_settings import get_system_settings

from .forms import ClaimItemForm
from .models import ClaimAttachment, ClaimRequest
from .models import ClaimApproval
from .views import notify, pending_action_title, telegram_action_keyboard, telegram_approval_callback_data


@override_settings(TELEGRAM_BOT_TOKEN="")
class ClaimDraftTests(TestCase):
    def setUp(self):
        # ตรึงวันที่เฉพาะชุดเดิมที่ทดสอบเรื่องอื่น ไม่ให้วันที่ตัวอย่างเก่าหมดอายุ
        self.policy_clock = patch('apps.claims.policies.current_date', return_value=date(2026, 9, 19))
        self.policy_clock.start()
        self.addCleanup(self.policy_clock.stop)
        self.media_directory = TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.requester = User.objects.create_user(username="requester", password="safe-test-password", department="IT")
        Budget.objects.create(owner=self.requester, department="IT", name="Office")
        for code in ("BU", "Technology", "BU-6000.00", "BU-4000.00"):
            BusinessUnit.objects.create(code=code, name=code)
        self.requester.signature.name = "signatures/test/signature.png"
        self.requester.save(update_fields=["signature"])
        self.supervisor = User.objects.create_user(username="supervisor", password="safe-test-password")
        self.supervisor.groups.add(Group.objects.get(name=SUPERVISOR_APPROVER))
        self.accounting_manager = User.objects.create_user(username="accounting-manager", password="safe-test-password")
        self.accountant = User.objects.create_user(username="accountant", password="safe-test-password")
        self.cfo = User.objects.create_user(username="cfo", password="safe-test-password")
        configuration = get_system_settings()
        configuration.cfo = self.cfo
        configuration.save(update_fields=["cfo"])
        for user in (self.supervisor, self.accounting_manager, self.accountant):
            user.signature.name = f"signatures/test/{user.username}.png"
            user.save(update_fields=["signature"])
        self.requester.accounting_manager = self.accounting_manager
        self.requester.accountant = self.accountant
        self.requester.save(update_fields=["accounting_manager", "accountant"])
        self.client.force_login(self.requester)

    def test_every_item_field_is_required_and_marked_in_new_and_added_rows(self):
        response = self.client.get(reverse("claim_create"))
        self.assertEqual(response.status_code, 200)
        for field_name in ("description", "business_unit", "department", "budget", "requested_amount"):
            label = response.context["formset"].forms[0][field_name].label
            self.assertContains(response, f'for="id_items-0-{field_name}">{label}:<span class="required"> *</span>')
            self.assertContains(response, f'for="id_items-__prefix__-{field_name}">{label}:<span class="required"> *</span>')

        complete_item = {
            "description": "รายการทดสอบ", "business_unit": "BU", "department": "IT",
            "budget": "Office", "requested_amount": "100.00",
        }
        for field_name in complete_item:
            incomplete_item = complete_item.copy()
            incomplete_item[field_name] = ""
            form = ClaimItemForm(data=incomplete_item)
            self.assertFalse(form.is_valid(), field_name)
            self.assertIn(field_name, form.errors)

        self.assertTrue(ClaimItemForm(data=complete_item).is_valid())

    def test_requester_can_save_draft_with_multiple_items(self):
        response = self.client.post(reverse("claim_create"), {
            "expense_month": "2026-10", "request_type": "ADVANCE", "request_date": "2026-09-19", "work_location": "HQ", "branch_name": "", "business_unit": "Technology", "department": "IT", "supervisor": self.supervisor.pk,
            "budget": "Office", "purpose": "ซื้อวัสดุสำนักงาน",
            "items-TOTAL_FORMS": "2", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "กระดาษ", "items-0-business_unit": "Technology", "items-0-department": "IT", "items-0-budget": "Office", "items-0-requested_amount": "120.50",
            "items-1-description": "ปากกา", "items-1-business_unit": "Technology", "items-1-department": "IT", "items-1-budget": "Office", "items-1-requested_amount": "79.50",
        })
        claim = ClaimRequest.objects.get()
        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        self.assertEqual(claim.status, ClaimRequest.Status.DRAFT)
        self.assertEqual(claim.requested_total, Decimal("200.00"))
        self.assertEqual(claim.items.count(), 2)

    def test_requester_can_edit_a_returned_claim(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, business_unit="Technology", department="IT",
            work_location="HQ", budget="Office", purpose="Returned claim", status=ClaimRequest.Status.RETURNED,
        )

        response = self.client.get(reverse("claim_edit", args=[claim.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "แก้ไขคำขอที่ส่งกลับ")

    def test_editing_draft_recalculates_total_and_cfo_threshold(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, accounting_manager=self.accounting_manager,
            accountant=self.accountant, cfo=self.cfo, request_type=ClaimRequest.RequestType.ADVANCE,
            business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="แก้ยอดแบบร่าง",
        )
        item = claim.items.create(
            description="อุปกรณ์", business_unit="Technology", department="IT", budget="Office",
            requested_amount=Decimal("7000.00"),
        )
        claim.refresh_requested_total()

        for amount, needs_cfo in (("6000.00", True), ("4000.00", False)):
            business_unit = f"BU-{amount}"
            response = self.client.post(reverse("claim_edit", args=[claim.pk]), {
                "expense_month": "2026-10", "request_type": "ADVANCE", "request_date": "2026-10-07", "work_location": "HQ",
                "branch_name": "", "supervisor": self.supervisor.pk, "purpose": "แก้ยอดแบบร่าง",
                "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
                "items-0-id": item.pk, "items-0-claim_request": claim.pk,
                "items-0-description": "อุปกรณ์", "items-0-business_unit": business_unit,
                "items-0-department": "IT", "items-0-budget": "Office",
                "items-0-requested_amount": amount,
            })
            self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
            claim.refresh_from_db()
            item.refresh_from_db()
            self.assertEqual(item.requested_amount, Decimal(amount))
            self.assertEqual(claim.business_unit, business_unit)
            self.assertEqual(claim.requested_total, Decimal(amount))
            self.assertEqual(claim.requires_cfo_approval, needs_cfo)

    def test_request_date_accepts_day_first_format(self):
        response = self.client.post(reverse("claim_create"), {
            "expense_month": "2026-10", "request_type": "ADVANCE", "request_date": "25/09/2026", "work_location": "HQ", "branch_name": "", "business_unit": "Technology", "department": "IT", "supervisor": self.supervisor.pk,
            "budget": "Office", "purpose": "Day first date", "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "Item", "items-0-business_unit": "Technology", "items-0-department": "IT", "items-0-budget": "Office", "items-0-requested_amount": "1.00",
        })

        claim = ClaimRequest.objects.get()
        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        self.assertEqual(str(claim.request_date), "2026-09-25")

    def test_high_value_draft_keeps_assigned_first_approver(self):
        executive = User.objects.create_user(username="draft-executive", password="safe-test-password")
        executive.groups.add(Group.objects.get(name=EXECUTIVE_APPROVER))
        response = self.client.post(reverse("claim_create"), {
            "expense_month": "2026-10", "request_type": "ADVANCE", "request_date": "2026-09-19", "work_location": "HQ", "branch_name": "", "business_unit": "Technology", "department": "IT", "supervisor": self.supervisor.pk, "executive": executive.pk, "budget": "Office", "purpose": "รายการยอดสูง",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "อุปกรณ์", "items-0-business_unit": "Technology", "items-0-department": "IT", "items-0-budget": "Office", "items-0-requested_amount": "5000.01",
        })
        claim = ClaimRequest.objects.get()
        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        self.assertEqual(claim.supervisor, self.supervisor)
        self.assertIsNone(claim.executive)

    def test_requester_cannot_select_self_as_supervisor(self):
        self.requester.groups.add(Group.objects.get(name=SUPERVISOR_APPROVER))
        response = self.client.post(reverse("claim_create"), {
            "request_date": "2026-09-19", "work_location": "HQ", "branch_name": "", "business_unit": "Technology", "department": "IT", "supervisor": self.requester.pk, "purpose": "ทดสอบ",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "รายการ", "items-0-requested_amount": "1.00",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ClaimRequest.objects.count(), 0)

    def test_all_request_header_fields_are_required(self):
        response = self.client.post(reverse("claim_create"), {
            "request_date": "2026-09-19", "work_location": "HQ", "branch_name": "", "business_unit": "", "department": "", "supervisor": self.supervisor.pk, "budget": "", "purpose": "",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "", "items-0-requested_amount": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ClaimRequest.objects.count(), 0)

    def test_requester_can_delete_own_draft(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="ทดสอบ"
        )
        response = self.client.post(reverse("claim_delete", args=[claim.pk]))
        self.assertRedirects(response, reverse("claim_list"))
        self.assertFalse(ClaimRequest.objects.filter(pk=claim.pk).exists())

    def test_requester_can_preview_an_attachment_inline(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="ทดสอบ"
        )
        attachment = ClaimAttachment.objects.create(
            claim_request=claim,
            original_name="receipt.jpg",
            uploaded_by=self.requester,
            file=SimpleUploadedFile("receipt.jpg", b"image-data", content_type="image/jpeg"),
        )
        response = self.client.get(reverse("claim_attachment_preview", args=[claim.pk, attachment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertIn("inline", response["Content-Disposition"])
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

        pdf = ClaimAttachment.objects.create(
            claim_request=claim,
            original_name="invoice.pdf",
            uploaded_by=self.requester,
            file=SimpleUploadedFile("invoice.pdf", b"%PDF-1.4", content_type="application/pdf"),
        )
        pdf_response = self.client.get(reverse("claim_attachment_preview", args=[claim.pk, pdf.pk]))
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")
        self.assertEqual(pdf_response["X-Frame-Options"], "SAMEORIGIN")

        detail = self.client.get(reverse("claim_detail", args=[claim.pk]))
        self.assertContains(detail, 'attachment-preview-trigger', html=False)
        self.assertContains(detail, 'id="attachment-preview-panel"', html=False)
        self.assertContains(detail, f'data-preview-url="{reverse("claim_attachment_preview", args=[claim.pk, attachment.pk])}"')
        self.assertNotContains(detail, 'target="_blank"')

    def test_attachment_list_identifies_requester_and_accountant_uploaders(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, accountant=self.accountant,
            business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="Attachment labels",
        )
        ClaimAttachment.objects.create(claim_request=claim, file=SimpleUploadedFile("requester.pdf", b"requester"), original_name="requester.pdf", uploaded_by=self.requester)
        ClaimAttachment.objects.create(claim_request=claim, file=SimpleUploadedFile("accountant.pdf", b"accountant"), original_name="accountant.pdf", uploaded_by=self.accountant)

        response = self.client.get(reverse("claim_detail", args=[claim.pk]))

        self.assertContains(response, "ผู้ขอแนบ: requester")
        self.assertContains(response, "บัญชีแนบ: accountant")

    def test_requester_cannot_delete_submitted_request(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="ทดสอบ",
            status=ClaimRequest.Status.PENDING_SUPERVISOR,
        )
        response = self.client.post(reverse("claim_delete", args=[claim.pk]))
        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        self.assertTrue(ClaimRequest.objects.filter(pk=claim.pk).exists())

    def test_stale_approval_action_redirects_to_latest_claim_status(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, accounting_manager=self.accounting_manager, accountant=self.accountant,
            business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="ทดสอบ",
            status=ClaimRequest.Status.PENDING_SUPERVISOR,
        )
        response = self.client.post(reverse("claim_approval_action", args=[claim.pk]), {"action": "approve"})
        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)

    def test_system_admin_can_delete_any_request(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="ทดสอบ",
            status=ClaimRequest.Status.PENDING_SUPERVISOR,
        )
        Notification.objects.create(recipient=self.supervisor, title="ทดสอบ", link=reverse("claim_detail", args=[claim.pk]))
        admin = User.objects.create_user(username="claim-admin", password="safe-test-password")
        admin.groups.add(Group.objects.get(name=SUPER_ADMIN))
        admin.signature.name = "signatures/test/claim-admin.png"
        admin.save(update_fields=["signature"])
        self.client.force_login(admin)

        response = self.client.post(reverse("claim_delete", args=[claim.pk]))

        self.assertRedirects(response, reverse("claim_list"))
        self.assertFalse(ClaimRequest.objects.filter(pk=claim.pk).exists())
        self.assertFalse(Notification.objects.filter(link=reverse("claim_detail", args=[claim.pk])).exists())

    def test_kanchanaburi_request_does_not_require_branch_name(self):
        response = self.client.post(reverse("claim_create"), {
            "expense_month": "2026-10", "request_type": "ADVANCE", "request_date": "2026-09-19", "work_location": "BRANCH", "branch_name": "", "business_unit": "Technology", "department": "IT", "supervisor": self.supervisor.pk, "budget": "Office", "purpose": "ทดสอบ",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "รายการ", "items-0-business_unit": "Technology", "items-0-department": "IT", "items-0-budget": "Office", "items-0-requested_amount": "1.00",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ClaimRequest.objects.count(), 1)

    def test_request_moves_from_submit_to_supervisor_then_accounting(self):
        configuration = get_system_settings()
        configuration.document_number_prefix = "PC"
        configuration.save(update_fields=["document_number_prefix"])
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, accounting_manager=self.accounting_manager, accountant=self.accountant,
            business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="ทดสอบ", expense_month="2026-10-01",
        )
        claim.items.create(description="รายการ", requested_amount="100.00")
        claim.refresh_requested_total()

        response = self.client.post(reverse("claim_submit", args=[claim.pk]))
        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.assertTrue(claim.document_number.startswith("HO-"))
        self.assertEqual(ClaimApproval.objects.count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.supervisor).count(), 1)

        self.client.force_login(self.supervisor)
        response = self.client.post(reverse("claim_approval_action", args=[claim.pk]), {"action": "approve", "comment": ""})
        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)

        self.client.force_login(self.accounting_manager)
        response = self.client.post(reverse("claim_approval_action", args=[claim.pk]), {"action": "approve", "comment": ""})
        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)


    def test_request_above_5000_goes_to_assigned_first_approver(self):
        executive = User.objects.create_user(username="executive", password="safe-test-password")
        executive.groups.add(Group.objects.get(name=EXECUTIVE_APPROVER))
        executive.signature.name = "signatures/test/executive.png"
        executive.onboarding_completed = True
        executive.save(update_fields=["signature", "onboarding_completed"])
        self.requester.executive = executive
        self.requester.save(update_fields=["executive"])
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, executive=executive,
            accounting_manager=self.accounting_manager, accountant=self.accountant,
            business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="High-value test", expense_month="2026-10-01",
        )
        claim.items.create(description="High-value item", requested_amount="5000.01")
        claim.refresh_requested_total()

        self.client.post(reverse("claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.assertEqual(claim.current_approver(), self.supervisor)
        self.assertEqual(Notification.objects.filter(recipient=self.supervisor).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=executive).count(), 0)

        self.client.force_login(self.supervisor)
        response = self.client.post(reverse("claim_approval_action", args=[claim.pk]), {"action": "approve", "comment": ""})
        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)

    def test_excess_actual_amount_requires_manager_approval_then_closes_after_payment(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor,
            accounting_manager=self.accounting_manager, accountant=self.accountant,
            business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="Excess test",
            status=ClaimRequest.Status.PENDING_CLEARANCE,
        )
        item = claim.items.create(description="Expense", requested_amount="100.00")
        claim.refresh_requested_total()

        self.client.force_login(self.requester)
        response = self.client.post(reverse("claim_clearance_submit", args=[claim.pk]), {f"actual_{item.pk}": "125.00", f"actual_description_{item.pk}": "ซื้อของจริง", "clearance_note": "ใช้เกิน", "excess_reason": "ราคาสูงขึ้น", "attachments": SimpleUploadedFile("excess-receipt.pdf", b"test receipt", content_type="application/pdf")})
        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_EXCESS_APPROVAL)
        self.assertEqual(claim.actual_total, Decimal("125.00"))
        self.assertTrue(claim.has_excess)

        self.client.force_login(self.accounting_manager)
        self.client.post(reverse("claim_approval_action", args=[claim.pk]), {"action": "approve", "comment": ""})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)

        self.client.force_login(self.accountant)
        self.client.post(reverse("claim_payment", args=[claim.pk]), {"payment_method": "CASH"})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.assertEqual(claim.approval_records.last().action, ClaimApproval.Action.SUPPLEMENTAL_PAID)
        payment = claim.payments.get()
        self.assertEqual(payment.kind, "SUPPLEMENTAL")
        self.assertEqual(payment.amount, Decimal("25.00"))
        self.assertEqual(payment.method, "CASH")

    def test_accountant_can_optionally_attach_files_when_completing_clearance(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor,
            accounting_manager=self.accounting_manager, accountant=self.accountant,
            business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="Clearance attachment",
            status=ClaimRequest.Status.PENDING_CLEARANCE,
        )
        item = claim.items.create(description="Expense", requested_amount="100.00")
        claim.refresh_requested_total()

        self.client.force_login(self.requester)
        response = self.client.post(
            reverse("claim_clearance_submit", args=[claim.pk]),
            {f"actual_{item.pk}": "80.00", f"actual_description_{item.pk}": "ซื้อของจริง", "clearance_note": "คืนเงินเหลือ", "attachments": SimpleUploadedFile("receipt.pdf", b"receipt", content_type="application/pdf")},
        )

        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        claim.refresh_from_db()
        attachment = claim.attachments.get()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW)
        self.assertEqual(attachment.original_name, "receipt.pdf")
        self.assertEqual(attachment.uploaded_by, self.requester)

        self.client.force_login(self.accountant)
        self.client.post(reverse("claim_complete", args=[claim.pk]), {"refund_confirmed": "1"})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)

    def test_accountant_can_cancel_clearance_only_after_confirming_full_refund(self):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor,
            accounting_manager=self.accounting_manager, accountant=self.accountant,
            business_unit="Technology", department="IT", work_location="HQ", budget="Office", purpose="คืนเงินเต็มจำนวน",
            requested_total="500.00", status=ClaimRequest.Status.PENDING_CLEARANCE,
        )
        self.client.force_login(self.accountant)

        missing_confirmation = self.client.post(reverse("claim_cancel_after_refund", args=[claim.pk]), {"cancel_reason": "ผู้ขอไม่ได้ใช้เงิน"})
        self.assertRedirects(missing_confirmation, reverse("claim_detail", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_CLEARANCE)

        response = self.client.post(reverse("claim_cancel_after_refund", args=[claim.pk]), {"refund_confirmed": "1", "cancel_reason": "ผู้ขอไม่ได้ใช้เงินและคืนเงินครบ"})
        self.assertRedirects(response, reverse("claim_detail", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.CANCELLED)
        self.assertEqual(claim.approval_records.last().action, ClaimApproval.Action.CANCELLED)
        self.assertIn("500.00", claim.approval_records.last().comment)
        self.assertTrue(Notification.objects.filter(recipient=self.requester, title="คำขอถูกยกเลิก").exists())
        self.assertTrue(AdminAuditLog.objects.filter(action="CANCEL_CLAIM").exists())

        dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(dashboard, "คืนเงินเต็มจำนวน")


class ClaimExportTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username="export-requester", password="safe-test-password")
        self.supervisor = User.objects.create_user(username="export-supervisor", password="safe-test-password")
        self.accountant = User.objects.create_user(username="export-accountant", password="safe-test-password")
        for user in (self.requester, self.accountant):
            user.signature.name = f"signatures/test/{user.username}.png"
            user.save(update_fields=["signature"])
        self.accountant.groups.add(Group.objects.get(name=ACCOUNTANT))
        self.claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, accountant=self.accountant,
            document_number="BR-EXPORT-01", business_unit="IT", department="IT", work_location="HQ",
            budget="Office", purpose="Exported claim", requested_total="123.45", status=ClaimRequest.Status.COMPLETED,
        )
        self.claim.items.create(description="Notebook", requested_amount="123.45")

    def test_accountant_can_export_visible_claims_to_excel(self):
        self.client.force_login(self.accountant)

        response = self.client.get(reverse("claim_export"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        workbook = load_workbook(BytesIO(response.content))
        worksheet = workbook.active
        self.assertEqual(worksheet["A6"].value, "BR-EXPORT-01")
        self.assertEqual(worksheet["H6"].value, "Exported claim")
        self.assertEqual(workbook.sheetnames, ["สรุปคำขอ", "รายการค่าใช้จ่าย", "ประวัติจ่ายเงิน"])
        self.assertEqual(workbook['รายการค่าใช้จ่าย']["F6"].value, "Notebook")
        self.assertTrue(AdminAuditLog.objects.filter(actor=self.accountant, action="EXPORT_CLAIMS").exists())

    def test_requester_cannot_export_claims(self):
        self.client.force_login(self.requester)

        response = self.client.get(reverse("claim_export"))

        self.assertEqual(response.status_code, 403)

    def test_super_admin_can_export_claims(self):
        admin = User.objects.create_user(username="export-admin", password="safe-test-password")
        admin.signature.name = "signatures/test/export-admin.png"
        admin.save(update_fields=["signature"])
        admin.groups.add(Group.objects.get(name=SUPER_ADMIN))
        self.client.force_login(admin)

        response = self.client.get(reverse("claim_export"))

        self.assertEqual(response.status_code, 200)


class TelegramApprovalWebhookTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username="telegram-requester", password="safe-test-password")
        self.duplicate = User.objects.create_user(username="telegram-duplicate", password="safe-test-password", telegram_user_id="9001")
        self.supervisor = User.objects.create_user(username="telegram-supervisor", password="safe-test-password", telegram_user_id="9001")
        self.manager = User.objects.create_user(username="telegram-manager", password="safe-test-password")
        self.accountant = User.objects.create_user(username="telegram-accountant", password="safe-test-password")
        self.claim = ClaimRequest.objects.create(
            requester=self.requester,
            supervisor=self.supervisor,
            accounting_manager=self.manager,
            accountant=self.accountant,
            business_unit="Technology",
            department="IT",
            work_location="HQ",
            budget="Office",
            purpose="Telegram approval test",
            status=ClaimRequest.Status.PENDING_SUPERVISOR,
        )
        self.claim.items.create(description="รายการ", business_unit="Technology", department="IT", budget="Office", requested_amount="100.00")
        self.claim.refresh_requested_total()

    @patch("apps.claims.views.answer_telegram_callback")
    @patch("apps.claims.views.send_telegram_notification")
    @patch("apps.claims.views.send_telegram_group_notification")
    def test_telegram_approval_uses_assigned_approver(self, group_notification, direct_notification, answer_callback):
        payload = {"callback_query": {"id": "callback-1", "data": telegram_approval_callback_data(self.claim, "approve"), "from": {"id": 9001}}}

        with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"):
            response = self.client.post(
                reverse("telegram_webhook"),
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret",
            )

        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        answer_callback.assert_called_once()
        self.assertIn("ยอดขอเบิก", direct_notification.call_args.args[2])
        self.assertIn("วัตถุประสงค์", direct_notification.call_args.args[2])
        self.assertIn("• รายการ: 100.00 บาท | Budget Office", direct_notification.call_args.args[2])
        self.assertIn("แผนก IT", direct_notification.call_args.args[2])
        self.assertIn("ผ่านการอนุมัติแล้ว: telegram-supervisor (ผู้อนุมัติ)", direct_notification.call_args.args[2])
        self.assertNotIn("Technology / IT", direct_notification.call_args.args[2])

    @patch("apps.claims.views.send_telegram_notification")
    @patch("apps.claims.views.send_telegram_group_notification")
    def test_notification_shows_requester_department_and_only_current_approval_round(self, group_notification, direct_notification):
        self.requester.department = "ฝ่ายขาย"
        self.requester.save(update_fields=["department"])
        self.claim.document_number = "BR-69101401"
        self.claim.status = ClaimRequest.Status.PENDING_ACCOUNTING
        self.claim.save(update_fields=["document_number", "status"])
        ClaimApproval.objects.create(
            claim_request=self.claim, actor=self.manager, action=ClaimApproval.Action.APPROVED,
            status_before=ClaimRequest.Status.PENDING_ACCOUNTING, status_after=ClaimRequest.Status.PENDING_PAYMENT,
        )
        ClaimApproval.objects.create(
            claim_request=self.claim, actor=self.requester, action=ClaimApproval.Action.SUBMITTED,
            status_before=ClaimRequest.Status.DRAFT, status_after=ClaimRequest.Status.PENDING_SUPERVISOR,
        )
        ClaimApproval.objects.create(
            claim_request=self.claim, actor=self.supervisor, action=ClaimApproval.Action.APPROVED,
            status_before=ClaimRequest.Status.PENDING_SUPERVISOR, status_after=ClaimRequest.Status.PENDING_ACCOUNTING,
        )

        notify(self.manager, "รอผู้จัดการบัญชีอนุมัติ", "คำขอรอการอนุมัติจากคุณ", self.claim)

        message = direct_notification.call_args.args[2]
        self.assertIn("ผู้ขอ: telegram-requester (แผนก ฝ่ายขาย)", message)
        self.assertIn("ผ่านการอนุมัติแล้ว: telegram-supervisor (ผู้อนุมัติ)", message)
        self.assertNotIn("telegram-manager (ผู้จัดการบัญชี)", message)
        self.assertIn("• รายการ: 100.00 บาท | Budget Office | แผนก IT", message)
        self.assertEqual(group_notification.call_args.args[1], message)

    @patch("apps.claims.views.answer_telegram_callback")
    @patch("apps.claims.views.send_telegram_notification")
    @patch("apps.claims.views.send_telegram_group_notification")
    def test_telegram_rejection_records_selected_reason(self, group_notification, direct_notification, answer_callback):
        payload = {"callback_query": {"id": "callback-2", "data": telegram_approval_callback_data(self.claim, "reject-budget"), "from": {"id": 9001}}}

        with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"):
            response = self.client.post(
                reverse("telegram_webhook"),
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret",
            )

        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimRequest.Status.REJECTED)
        self.assertIn("งบประมาณ", self.claim.approval_records.last().comment)

    @patch("apps.claims.views.answer_telegram_callback")
    @patch("apps.claims.views.send_telegram_notification")
    def test_telegram_reject_button_shows_reason_choices_before_rejecting(self, direct_notification, answer_callback):
        payload = {"callback_query": {"id": "callback-3", "data": telegram_approval_callback_data(self.claim, "reject-menu"), "from": {"id": 9001}}}

        with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"):
            response = self.client.post(
                reverse("telegram_webhook"),
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret",
            )

        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        markup = direct_notification.call_args.kwargs["reply_markup"]
        self.assertEqual(len(markup["inline_keyboard"]), 4)
        self.assertEqual(markup["inline_keyboard"][2][0]["text"], "กรุณาติดต่อฝ่ายบัญชี")
        self.assertEqual(markup["inline_keyboard"][3][0]["callback_data"], telegram_approval_callback_data(self.claim, "approval-menu"))

    @patch("apps.claims.views.remove_telegram_callback_keyboard")
    @patch("apps.claims.views.answer_telegram_callback")
    @patch("apps.claims.views.send_telegram_notification")
    @patch("apps.claims.views.send_telegram_group_notification")
    def test_old_telegram_payment_button_cannot_bypass_method_and_slip(self, group_notification, direct_notification, answer_callback, remove_keyboard):
        self.accountant.telegram_user_id = "9002"
        self.accountant.save(update_fields=["telegram_user_id"])
        self.claim.status = ClaimRequest.Status.PENDING_PAYMENT
        self.claim.save(update_fields=["status"])
        payload = {
            "callback_query": {
                "id": "callback-payment",
                "data": f"pc:{self.claim.id}:pay",
                "from": {"id": 9002},
                "message": {"chat": {"id": 9002}, "message_id": 88},
            }
        }

        with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"):
            response = self.client.post(
                reverse("telegram_webhook"),
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret",
            )

        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertFalse(self.claim.approval_records.filter(action=ClaimApproval.Action.PAID).exists())
        answer_callback.assert_called_with("callback-payment", "กรุณาเปิดหน้าเว็บเพื่อเลือกวิธีจ่ายเงินและแนบสลิปเมื่อโอน")
        remove_keyboard.assert_called_once_with(payload["callback_query"])

    def test_telegram_manager_receives_approval_buttons(self):
        self.claim.status = ClaimRequest.Status.PENDING_ACCOUNTING

        keyboard = telegram_action_keyboard(self.claim)

        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], telegram_approval_callback_data(self.claim, "approve"))
        self.assertEqual(keyboard["inline_keyboard"][0][1]["callback_data"], telegram_approval_callback_data(self.claim, "reject-menu"))

    def test_telegram_accountant_receives_link_to_payment_form(self):
        self.claim.status = ClaimRequest.Status.PENDING_PAYMENT

        with self.settings(PUBLIC_BASE_URL="https://petty.example.com"):
            keyboard = telegram_action_keyboard(self.claim)

        self.assertEqual(keyboard["inline_keyboard"][0][0]["url"], f"https://petty.example.com/claims/{self.claim.id}/")

    @patch("apps.claims.views.remove_telegram_callback_keyboard")
    @patch("apps.claims.views.answer_telegram_callback")
    @patch("apps.claims.views.send_telegram_notification")
    @patch("apps.claims.views.send_telegram_group_notification")
    def test_telegram_requester_can_confirm_receipt(self, group_notification, direct_notification, answer_callback, remove_keyboard):
        self.requester.telegram_user_id = "9003"
        self.requester.save(update_fields=["telegram_user_id"])
        self.claim.status = ClaimRequest.Status.PAID
        self.claim.save(update_fields=["status"])
        payload = {
            "callback_query": {
                "id": "callback-receipt",
                "data": f"pc:{self.claim.id}:receive",
                "from": {"id": 9003},
                "message": {"chat": {"id": 9003}, "message_id": 99},
            }
        }

        with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"):
            response = self.client.post(
                reverse("telegram_webhook"),
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret",
            )

        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimRequest.Status.PENDING_CLEARANCE)
        self.assertEqual(self.claim.approval_records.last().action, ClaimApproval.Action.RECEIVED)
        remove_keyboard.assert_called_once_with(payload["callback_query"])

    def test_telegram_requester_receives_receipt_button(self):
        self.claim.status = ClaimRequest.Status.PAID

        keyboard = telegram_action_keyboard(self.claim)

        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], f"pc:{self.claim.id}:receive")

    @patch("apps.claims.views.remove_telegram_callback_keyboard")
    @patch("apps.claims.views.answer_telegram_callback")
    @patch("apps.claims.views.send_telegram_notification")
    @patch("apps.claims.views.send_telegram_group_notification")
    def test_telegram_petty_cash_receipt_closes_request(self, group_notification, direct_notification, answer_callback, remove_keyboard):
        self.requester.telegram_user_id = "9003"
        self.requester.save(update_fields=["telegram_user_id"])
        self.claim.request_type = ClaimRequest.RequestType.PETTY_CASH
        self.claim.status = ClaimRequest.Status.PAID
        self.claim.save(update_fields=["request_type", "status"])
        payload = {"callback_query": {
            "id": "petty-receipt", "data": f"pc:{self.claim.pk}:receive", "from": {"id": 9003},
            "message": {"chat": {"id": 9003}, "message_id": 100},
        }}
        with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"):
            response = self.client.post(
                reverse("telegram_webhook"), data=json.dumps(payload), content_type="application/json",
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret",
            )
        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimRequest.Status.COMPLETED)
        self.assertTrue(self.claim.approval_records.filter(action=ClaimApproval.Action.RECEIVED).exists())
        remove_keyboard.assert_called_once_with(payload["callback_query"])

    def test_accounting_step_uses_manager_specific_title(self):
        self.claim.status = ClaimRequest.Status.PENDING_ACCOUNTING

        self.assertEqual(pending_action_title(self.claim), "รอผู้จัดการบัญชีอนุมัติ")

    @patch("apps.claims.views.remove_telegram_callback_keyboard")
    @patch("apps.claims.views.answer_telegram_callback")
    def test_stale_telegram_button_is_removed_after_web_action(self, answer_callback, remove_keyboard):
        self.claim.status = ClaimRequest.Status.PENDING_PAYMENT
        self.claim.save(update_fields=["status"])
        payload = {
            "callback_query": {
                "id": "callback-stale",
                "data": f"pc:{self.claim.id}:approve",
                "from": {"id": 9001},
                "message": {"chat": {"id": 9001}, "message_id": 77},
            }
        }

        with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"):
            response = self.client.post(
                reverse("telegram_webhook"),
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret",
            )

        self.assertEqual(response.status_code, 200)
        remove_keyboard.assert_called_once_with(payload["callback_query"])
        answer_callback.assert_called_once_with("callback-stale", "ปุ่มอนุมัติรุ่นเก่าหมดอายุ กรุณาเปิดหน้าเว็บเพื่อทำรายการ")
