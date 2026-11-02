from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from unittest.mock import patch

from django.core.management import call_command
from django.contrib.auth.models import Group
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from django.urls import reverse
from openpyxl import Workbook

from apps.accounts.models import User
from apps.accounts.roles import ACCOUNTANT, ACCOUNTING_APPROVER
from apps.claims.models import ClaimApproval, ClaimRequest
from .models import AdminAuditLog, Budget, BusinessUnit, Notification, TelegramWebhookCheck
from .system_settings import get_system_settings
from .forms import SystemSettingsForm


class SystemSettingsFeatureToggleTests(TestCase):
    def test_location_prefix_fields_normalize_validate_and_save(self):
        configuration = get_system_settings()
        self.assertEqual(configuration.head_office_document_prefix, 'HO')
        self.assertEqual(configuration.branch_document_prefix, 'BR')
        form = SystemSettingsForm(instance=configuration)
        data = {name: form[name].value() for name in form.fields if form[name].value() is not None}
        data.update(head_office_document_prefix='hq', branch_document_prefix='kan')
        form = SystemSettingsForm(data, instance=configuration)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        configuration.refresh_from_db()
        self.assertEqual(configuration.head_office_document_prefix, 'HQ')
        self.assertEqual(configuration.branch_document_prefix, 'KAN')
        for office, branch, error_field in [('ho', 'HO', 'branch_document_prefix'), ('HO-', 'BR', 'head_office_document_prefix'), ('HO', '', 'branch_document_prefix'), ('A' * 13, 'BR', 'head_office_document_prefix')]:
            data.update(head_office_document_prefix=office, branch_document_prefix=branch)
            form = SystemSettingsForm(data, instance=configuration)
            self.assertFalse(form.is_valid())
            self.assertIn(error_field, form.errors)

    def test_new_settings_keep_existing_features_enabled_by_default(self):
        configuration = get_system_settings()
        self.assertTrue(configuration.require_signature_on_first_login)
        self.assertTrue(configuration.claim_print_enabled)
        self.assertTrue(configuration.auto_receive_enabled)
        self.assertEqual(configuration.auto_receive_after_days, 1)
        self.assertEqual(configuration.petty_receive_after_days, 3)

    def test_admin_can_save_both_feature_switches(self):
        admin = User.objects.create_superuser(username="settings-toggle-admin", password="safe-test-password")
        admin.signature.name = "signatures/test/settings-toggle-admin.png"
        admin.save(update_fields=["signature"])
        self.client.force_login(admin)
        page = self.client.get(reverse("system_settings"))
        self.assertContains(page, "บังคับเพิ่มลายเซ็นก่อนใช้งานครั้งแรก")
        self.assertContains(page, "เปิดใช้งานเอกสารสำหรับพิมพ์")
        self.assertContains(page, 'รหัสเอกสารสำนักงานใหญ่')
        self.assertContains(page, 'รหัสเอกสารกาญจนบุรี')
        configuration = get_system_settings()
        form = SystemSettingsForm(instance=configuration)
        data = {name: form[name].value() for name in form.fields if form[name].value() is not None}
        data.pop("require_signature_on_first_login", None)
        data.pop("claim_print_enabled", None)

        response = self.client.post(reverse("system_settings"), data)

        self.assertRedirects(response, reverse("system_settings"))
        configuration.refresh_from_db()
        self.assertFalse(configuration.require_signature_on_first_login)
        self.assertFalse(configuration.claim_print_enabled)

        data["require_signature_on_first_login"] = "on"
        data["claim_print_enabled"] = "on"
        self.assertRedirects(self.client.post(reverse("system_settings"), data), reverse("system_settings"))
        configuration.refresh_from_db()
        self.assertTrue(configuration.require_signature_on_first_login)
        self.assertTrue(configuration.claim_print_enabled)

    def test_admin_can_configure_auto_receipt_window(self):
        admin = User.objects.create_superuser(username="auto-receipt-settings-admin", password="safe-test-password")
        admin.signature.name = "signatures/test/auto-receipt-settings-admin.png"
        admin.save(update_fields=["signature"])
        self.client.force_login(admin)
        configuration = get_system_settings()
        form = SystemSettingsForm(instance=configuration)
        data = {name: form[name].value() for name in form.fields if form[name].value() is not None}
        data.pop("auto_receive_enabled", None)
        data["auto_receive_after_days"] = "5"
        data["petty_receive_after_days"] = "10"

        self.assertRedirects(self.client.post(reverse("system_settings"), data), reverse("system_settings"))
        configuration.refresh_from_db()
        self.assertFalse(configuration.auto_receive_enabled)
        self.assertEqual(configuration.auto_receive_after_days, 5)
        self.assertEqual(configuration.petty_receive_after_days, 10)

        data["auto_receive_enabled"] = "on"
        data["auto_receive_after_days"] = "0"
        response = self.client.post(reverse("system_settings"), data)
        self.assertContains(response, "เบิกยืมเงิน: รอกดยืนยันรับเงิน")
        configuration.refresh_from_db()
        self.assertEqual(configuration.auto_receive_after_days, 5)

        data["auto_receive_after_days"] = "3"
        self.assertRedirects(self.client.post(reverse("system_settings"), data), reverse("system_settings"))
        configuration.refresh_from_db()
        self.assertTrue(configuration.auto_receive_enabled)
        self.assertEqual(configuration.auto_receive_after_days, 3)
        self.assertEqual(configuration.petty_receive_after_days, 10)


class LogRetentionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="retention-user", password="test-password")

    def test_cleanup_uses_configured_retention_period(self):
        old_audit = AdminAuditLog.objects.create(actor=self.user, action="LOGIN_LOCAL")
        recent_audit = AdminAuditLog.objects.create(actor=self.user, action="LOGIN_LOCAL")
        old_notification = Notification.objects.create(recipient=self.user, title="เก่า")
        recent_notification = Notification.objects.create(recipient=self.user, title="ใหม่")
        configuration = get_system_settings()
        configuration.log_retention_days = 7
        configuration.save(update_fields=["log_retention_days"])
        old_time = timezone.now() - timedelta(days=8)
        AdminAuditLog.objects.filter(pk=old_audit.pk).update(created_at=old_time)
        Notification.objects.filter(pk=old_notification.pk).update(created_at=old_time)

        call_command("cleanup_expired_logs")

        self.assertFalse(AdminAuditLog.objects.filter(pk=old_audit.pk).exists())
        self.assertTrue(AdminAuditLog.objects.filter(pk=recent_audit.pk).exists())
        self.assertFalse(Notification.objects.filter(pk=old_notification.pk).exists())
        self.assertTrue(Notification.objects.filter(pk=recent_notification.pk).exists())


class BudgetSettingsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="budget-user", password="safe-test-password", department="IT")
        self.user.groups.add(Group.objects.get(name=ACCOUNTANT))
        self.user.signature.name = "signatures/test/budget-user.png"
        self.user.save(update_fields=["signature"])
        self.client.force_login(self.user)

    def test_user_can_create_multiple_budgets_then_update_and_delete_with_audit_log(self):
        response = self.client.post(reverse("budget_settings"), {"department": "IT", "name": "ค่าใช้จ่ายสำนักงาน", "category": Budget.Category.EXPENSE})

        self.assertRedirects(response, reverse("budget_settings"))
        budget = Budget.objects.get(owner=self.user, name="ค่าใช้จ่ายสำนักงาน")
        self.assertEqual(budget.category, Budget.Category.EXPENSE)
        self.assertTrue(AdminAuditLog.objects.filter(actor=self.user, action="CREATE_BUDGET", target="ค่าใช้จ่ายสำนักงาน").exists())

        response = self.client.post(reverse("budget_settings"), {"department": "IT", "name": "ค่าเดินทาง", "category": Budget.Category.EXPENSE})

        self.assertRedirects(response, reverse("budget_settings"))
        self.assertEqual(Budget.objects.filter(owner=self.user).count(), 2)
        page = self.client.get(reverse("budget_settings"))
        self.assertNotContains(page, "งบประมาณเทียบเป็นรายปี")

        response = self.client.post(reverse("budget_settings"), {"budget_id": budget.id, "department": "IT", "name": "ค่าใช้จ่ายสำนักงาน 2026", "category": Budget.Category.ASSET})

        self.assertRedirects(response, reverse("budget_settings"))
        budget.refresh_from_db()
        self.assertEqual(budget.category, Budget.Category.ASSET)
        self.assertTrue(AdminAuditLog.objects.filter(actor=self.user, action="UPDATE_BUDGET", target="ค่าใช้จ่ายสำนักงาน 2026").exists())

        response = self.client.post(reverse("budget_delete", args=[budget.id]))

        self.assertRedirects(response, reverse("budget_settings"))
        self.assertFalse(Budget.objects.filter(pk=budget.id).exists())
        self.assertTrue(AdminAuditLog.objects.filter(actor=self.user, action="DELETE_BUDGET", target="ค่าใช้จ่ายสำนักงาน 2026").exists())

    def test_budget_department_is_required_and_new_budget_defaults_to_expense(self):
        response = self.client.post(reverse("budget_settings"), {"name": "ไม่มีแผนก", "category": Budget.Category.EXPENSE})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Budget.objects.filter(owner=self.user).exists())

        budget = Budget.objects.create(owner=self.user, department="IT", name="งบใหม่")
        self.assertEqual(budget.category, Budget.Category.EXPENSE)

    def test_finance_can_add_and_edit_bu_distribution_rule(self):
        response = self.client.post(reverse("budget_settings"), {
            "form_type": "business_unit", "code": "ECOM", "name": "E-commerce",
        })
        self.assertRedirects(response, reverse("budget_settings"))
        unit = BusinessUnit.objects.get(code="ECOM")
        self.assertContains(self.client.get(reverse("budget_settings")), "ECOM")
        response = self.client.post(reverse("budget_settings"), {
            "form_type": "business_unit", "bu_id": unit.pk, "code": "ECOM", "name": "Online",
        })
        self.assertRedirects(response, reverse("budget_settings"))
        unit.refresh_from_db()
        self.assertEqual(unit.name, "Online")
        self.assertTrue(AdminAuditLog.objects.filter(action="UPDATE_BU", target="ECOM").exists())

    def test_regular_user_only_sees_own_department_and_cannot_change_budgets(self):
        hr = User.objects.create_user(username="hr-user", password="safe-test-password", department="HR")
        hr.signature.name = "signatures/test/hr-user.png"
        hr.save(update_fields=["signature"])
        BusinessUnit.objects.create(code="ECOM", name="E-commerce")
        Budget.objects.create(owner=self.user, department="IT", name="IT Office")
        other = Budget.objects.create(owner=self.user, department="HR", name="HR Office")
        self.client.force_login(hr)
        response = self.client.get(reverse("budget_settings"))
        self.assertContains(response, "HR Office")
        self.assertNotContains(response, "IT Office")
        self.assertNotContains(response, "เพิ่ม Budget")
        self.assertNotContains(response, "ECOM")
        self.assertNotContains(response, "bu-master-search")
        self.assertNotContains(response, "Distribution Rule")
        self.assertContains(response, "<h1>Budget</h1>", html=True)
        self.assertFalse(response.context['business_units'].exists())
        self.assertEqual(self.client.post(reverse("budget_settings"), {"department": "HR", "name": "Fake", "category": "Expense"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("budget_settings"), {"form_type": "business_unit", "code": "NO", "name": "No"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("budget_delete", args=[other.pk])).status_code, 403)

    def test_accounting_manager_cfo_and_admin_can_edit_master_data(self):
        manager = User.objects.create_user(username="master-manager", password="safe-test-password")
        manager.groups.add(Group.objects.get(name=ACCOUNTING_APPROVER))
        cfo = User.objects.create_user(username="master-cfo", password="safe-test-password")
        config = get_system_settings()
        config.cfo = cfo
        config.save(update_fields=["cfo"])
        admin = User.objects.create_superuser(username="master-admin", password="safe-test-password")
        for user, code in ((manager, "MGR"), (cfo, "CFO"), (admin, "ADMIN")):
            user.signature.name = f"signatures/test/{code}.png"
            user.save(update_fields=["signature"])
            self.client.force_login(user)
            response = self.client.post(reverse("budget_settings"), {
                "form_type": "business_unit", "code": code, "name": code,
            })
            self.assertRedirects(response, reverse("budget_settings"))
            self.assertTrue(BusinessUnit.objects.filter(code=code).exists())

    def test_finance_sees_budget_before_bu_in_both_editors_and_tables(self):
        BusinessUnit.objects.create(code='VISIBLE_BU', name='Finance only unit')
        Budget.objects.create(owner=self.user, department='IT', name='IT Budget')
        manager = User.objects.create_user(username='visibility-manager')
        manager.groups.add(Group.objects.get(name=ACCOUNTING_APPROVER))
        cfo = User.objects.create_user(username='visibility-cfo')
        config = get_system_settings()
        config.cfo = cfo
        config.save(update_fields=['cfo'])
        admin = User.objects.create_superuser(username='visibility-admin')
        for user in (self.user, manager, cfo, admin):
            user.signature.name = 'signatures/test/master-visibility.png'
            user.save(update_fields=['signature'])
            self.client.force_login(user)
            response = self.client.get(reverse('budget_settings'))
            self.assertContains(response, 'VISIBLE_BU')
            self.assertContains(response, 'IT Budget')
            body = response.content.decode()
            self.assertLess(body.index('budget-master-search'), body.index('bu-master-search'))
            self.assertLess(body.index('+ เพิ่ม Budget'), body.index('+ เพิ่ม BU'))

    def test_import_command_replaces_old_master_rows_without_budget_amounts(self):
        Budget.objects.create(owner=self.user, department="IT", name="Old Budget")
        BusinessUnit.objects.create(code="OLD", name="Old BU")
        workbook = Workbook()
        bu_sheet = workbook.active
        bu_sheet.title = "Business unit"
        bu_sheet.append(["Distribution Rule", "Distribution Rule Name"])
        bu_sheet.append(["ECOM", "E-commerce"])
        budget_sheet = workbook.create_sheet("Budget")
        budget_sheet.append(["Expenses/Asset", "Description", "Department"])
        budget_sheet.append(["Expense", "Office", "IT"])
        with TemporaryDirectory() as folder:
            path = Path(folder) / "master.xlsx"
            workbook.save(path)
            workbook.close()
            call_command("import_budget_master", str(path), dry_run=True, stdout=StringIO())
            self.assertTrue(Budget.objects.filter(name="Old Budget").exists())
            call_command("import_budget_master", str(path), owner=self.user.username, replace=True, stdout=StringIO())
        self.assertEqual(list(BusinessUnit.objects.values_list("code", flat=True)), ["ECOM"])
        self.assertEqual(list(Budget.objects.values_list("department", "name", "category")), [("IT", "Office", "Expense")])


class ClearTestClaimsCommandTests(TestCase):
    def test_requires_exact_claim_ids_and_keeps_unlisted_requests(self):
        actor = User.objects.create_superuser(username="clear-admin", password="safe-test-password")
        requester = User.objects.create_user(username="clear-requester", password="safe-test-password")
        test_claim = ClaimRequest.objects.create(requester=requester, work_location="HQ", purpose="test")
        other_claim = ClaimRequest.objects.create(requester=requester, work_location="HQ", purpose="keep")
        Notification.objects.create(recipient=requester, title="test", link=f"/claims/{test_claim.pk}/")
        Notification.objects.create(recipient=requester, title="keep", link=f"/claims/{other_claim.pk}/")
        with self.assertRaisesMessage(Exception, "รายการไม่ตรง"):
            call_command("clear_test_claims", ids=[test_claim.pk, other_claim.pk + 100], requester=requester.username,
                         actor=actor.username, expected_count=2, confirm=True, stdout=StringIO())
        self.assertEqual(ClaimRequest.objects.count(), 2)
        call_command("clear_test_claims", ids=[test_claim.pk], requester=requester.username,
                     actor=actor.username, expected_count=1, confirm=True, stdout=StringIO())
        self.assertFalse(ClaimRequest.objects.filter(pk=test_claim.pk).exists())
        self.assertTrue(ClaimRequest.objects.filter(pk=other_claim.pk).exists())
        self.assertEqual(Notification.objects.count(), 1)


class DashboardTests(TestCase):
    def test_dashboard_redirects_anonymous_user_to_login(self):
        response = self.client.get(reverse("dashboard"))

        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_authenticated_user_can_open_dashboard(self):
        user = User.objects.create_user(username="tester", password="safe-test-password", first_name="ผู้ทดสอบ")
        user.signature.name = "signatures/test/signature.png"
        user.save(update_fields=["signature"])
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "สวัสดี, ผู้ทดสอบ")

    def test_dashboard_counts_requesters_completed_claims(self):
        user = User.objects.create_user(username="requester", password="safe-test-password")
        user.signature.name = "signatures/test/requester.png"
        user.save(update_fields=["signature"])
        supervisor = User.objects.create_user(username="supervisor", password="safe-test-password")
        ClaimRequest.objects.create(
            requester=user,
            supervisor=supervisor,
            business_unit="IT",
            department="IT",
            work_location="HQ",
            budget="Office",
            purpose="รายการเสร็จสิ้น",
            status=ClaimRequest.Status.COMPLETED,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.context["own_claim_count"], 1)
        self.assertEqual(response.context["completed_count"], 1)

    def test_requester_sees_seven_relevant_cards_with_matching_lists(self):
        requester = User.objects.create_user(username="simple-requester", password="safe-test-password")
        requester.signature.name = "signatures/test/simple-requester.png"
        requester.save(update_fields=["signature"])
        other = User.objects.create_user(username="another-requester", password="safe-test-password")
        statuses = (
            ClaimRequest.Status.DRAFT,
            ClaimRequest.Status.PENDING_SUPERVISOR,
            ClaimRequest.Status.PENDING_ACCOUNTING,
            ClaimRequest.Status.PENDING_PAYMENT,
            ClaimRequest.Status.PAID,
            ClaimRequest.Status.PENDING_CLEARANCE,
            ClaimRequest.Status.RETURNED,
            ClaimRequest.Status.COMPLETED,
            ClaimRequest.Status.REJECTED,
            ClaimRequest.Status.CANCELLED,
        )
        for status in statuses:
            ClaimRequest.objects.create(
                requester=requester, work_location="HQ", purpose=status, status=status,
            )
        ClaimRequest.objects.create(
            requester=other, work_location="HQ", purpose="Other request",
            status=ClaimRequest.Status.PENDING_SUPERVISOR,
        )
        self.client.force_login(requester)

        response = self.client.get(reverse("dashboard"))

        self.assertTrue(response.context["requester_summary"])
        cards = response.context["status_cards"]
        self.assertEqual([card["label"] for card in cards], [
            "แบบร่าง", "กำลังดำเนินการ", "รอยืนยันรับเงิน", "รอลงยอดใช้จริง",
            "เสร็จสิ้น", "ส่งกลับแก้ไข", "ไม่อนุมัติ/ยกเลิก",
        ])
        self.assertEqual([card["count"] for card in cards], [1, 3, 1, 1, 1, 1, 2])
        self.assertEqual(response.context["draft_count"], 1)
        self.assertEqual(response.context["completed_count"], 1)
        for card in cards:
            listing = self.client.get(card["url"])
            self.assertEqual(listing.context["page_obj"].paginator.count, card["count"])
            self.assertTrue(all(claim.requester_id == requester.pk for claim in listing.context["claims"]))

    def test_cfo_cards_count_real_pending_actions_and_open_matching_claims(self):
        admin = User.objects.create_superuser(username="pipeline-admin", password="safe-test-password")
        cfo = User.objects.create_user(username="pipeline-cfo", password="safe-test-password")
        requester = User.objects.create_user(username="pipeline-employee", password="safe-test-password")
        for user in (admin, cfo, requester):
            user.signature.name = f"signatures/test/{user.username}.png"
            user.save(update_fields=["signature"])
        approval = ClaimRequest.objects.create(
            requester=requester, cfo=cfo, work_location="HQ", purpose="CFO ต้องอนุมัติ",
            requested_total=Decimal("6000.00"), approval_threshold=Decimal("5000.00"),
            status=ClaimRequest.Status.PENDING_ACCOUNTING, accounting_approved_at=timezone.now(),
        )
        initial_ack = ClaimRequest.objects.create(
            requester=requester, cfo=cfo, work_location="HQ", purpose="CFO รับทราบคำขอ",
            requested_total=Decimal("100.00"), approval_threshold=Decimal("5000.00"),
            status=ClaimRequest.Status.PENDING_PAYMENT, accounting_approved_at=timezone.now(),
        )
        excess_ack = ClaimRequest.objects.create(
            requester=requester, cfo=cfo, work_location="HQ", purpose="CFO รับทราบยอดเกิน",
            requested_total=Decimal("100.00"), actual_total=Decimal("125.00"),
            approval_threshold=Decimal("5000.00"), status=ClaimRequest.Status.PENDING_PAYMENT,
            accounting_approved_at=timezone.now(),
        )
        accounting = ClaimRequest.objects.create(
            requester=requester, cfo=cfo, work_location="HQ", purpose="รอผู้จัดการบัญชี",
            requested_total=Decimal("100.00"), status=ClaimRequest.Status.PENDING_ACCOUNTING,
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("dashboard"))
        cards = {card["code"]: card for card in response.context["status_cards"]}
        self.assertEqual(len(cards), 15)
        self.assertNotIn(ClaimRequest.Status.PENDING_EXECUTIVE, cards)
        self.assertEqual(cards[ClaimRequest.Status.PENDING_ACCOUNTING]["count"], 1)
        self.assertEqual(cards["CFO_APPROVAL"]["count"], 1)
        self.assertEqual(cards["CFO_ACKNOWLEDGEMENT"]["count"], 2)
        for code, expected_ids in (
            (ClaimRequest.Status.PENDING_ACCOUNTING, {accounting.pk}),
            ("CFO_APPROVAL", {approval.pk}),
            ("CFO_ACKNOWLEDGEMENT", {initial_ack.pk, excess_ack.pk}),
        ):
            listing = self.client.get(cards[code]["url"])
            self.assertEqual({claim.pk for claim in listing.context["claims"]}, expected_ids)
        self.client.force_login(cfo)
        response = self.client.get(reverse("dashboard"))
        cfo_cards = {card["code"]: card for card in response.context["status_cards"]}
        self.assertEqual(cfo_cards["CFO_APPROVAL"]["count"], 1)
        self.assertEqual(cfo_cards["CFO_ACKNOWLEDGEMENT"]["count"], 2)
        listing = self.client.get(cfo_cards["CFO_ACKNOWLEDGEMENT"]["url"])
        self.assertEqual({claim.pk for claim in listing.context["claims"]}, {initial_ack.pk, excess_ack.pk})
        # One claim may need both acknowledgements, but the card counts claims.
        excess_ack.cfo_acknowledged_at = timezone.now()
        excess_ack.save(update_fields=["cfo_acknowledged_at"])
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(next(card for card in response.context["status_cards"] if card["code"] == "CFO_ACKNOWLEDGEMENT")["count"], 2)
        excess_ack.cfo_excess_acknowledged_at = timezone.now()
        excess_ack.save(update_fields=["cfo_excess_acknowledged_at"])
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(next(card for card in response.context["status_cards"] if card["code"] == "CFO_ACKNOWLEDGEMENT")["count"], 1)

    def test_pipeline_counts_only_own_claims_for_non_admin(self):
        supervisor = User.objects.create_user(username="pipeline-supervisor", password="safe-test-password")
        supervisor.signature.name = "signatures/test/pipeline-supervisor.png"
        supervisor.save(update_fields=["signature"])
        requester = User.objects.create_user(username="pipeline-requester", password="safe-test-password")
        claim = ClaimRequest.objects.create(
            requester=requester, supervisor=supervisor, business_unit="IT", department="IT", work_location="HQ",
            budget="Office", purpose="Handled claim", status=ClaimRequest.Status.COMPLETED,
        )
        ClaimApproval.objects.create(
            claim_request=claim, actor=supervisor, action=ClaimApproval.Action.APPROVED,
            status_before=ClaimRequest.Status.PENDING_SUPERVISOR, status_after=ClaimRequest.Status.COMPLETED,
        )
        self.client.force_login(supervisor)

        response = self.client.get(reverse("dashboard"))

        completed_card = next(card for card in response.context["status_cards"] if card["code"] == ClaimRequest.Status.COMPLETED)
        self.assertEqual(completed_card["count"], 0)
        self.assertIn("scope=completed", completed_card["url"])
        listing = self.client.get(completed_card["url"])
        self.assertEqual(list(listing.context["claims"]), [])

    def test_assigned_workflow_cards_include_their_work_and_open_matching_lists(self):
        requester = User.objects.create_user(username="pipeline-owner", password="safe-test-password")
        approver = User.objects.create_user(username="pipeline-approver", password="safe-test-password")
        manager = User.objects.create_user(username="pipeline-manager", password="safe-test-password")
        accountant = User.objects.create_user(username="pipeline-accountant", password="safe-test-password")
        for user in (approver, manager, accountant):
            user.signature.name = f"signatures/test/{user.username}.png"
            user.save(update_fields=["signature"])
        assignments = (
            (approver, ClaimRequest.Status.PENDING_SUPERVISOR, {"supervisor": approver}),
            (manager, ClaimRequest.Status.PENDING_ACCOUNTING, {"accounting_manager": manager}),
            (manager, ClaimRequest.Status.PENDING_EXCESS_APPROVAL, {"accounting_manager": manager}),
            (accountant, ClaimRequest.Status.PENDING_PAYMENT, {"accountant": accountant}),
            (accountant, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW, {"accountant": accountant}),
        )
        claims = {}
        for user, status, assigned_to in assignments:
            claims[status] = ClaimRequest.objects.create(
                requester=requester, work_location="HQ", purpose=status,
                status=status, **assigned_to,
            )

        for user, status, _ in assignments:
            self.client.force_login(user)
            response = self.client.get(reverse("dashboard"))
            card = next(card for card in response.context["status_cards"] if card["code"] == status)
            self.assertEqual(card["count"], 1, status)
            listing = self.client.get(card["url"])
            self.assertEqual({claim.pk for claim in listing.context["claims"]}, {claims[status].pk}, status)

    def test_every_pipeline_card_count_matches_its_list(self):
        user = User.objects.create_user(username="all-cards-user", password="safe-test-password")
        user.signature.name = "signatures/test/all-cards-user.png"
        user.save(update_fields=["signature"])
        requester = User.objects.create_user(username="all-cards-requester", password="safe-test-password")
        ClaimRequest.objects.create(
            requester=user, work_location="HQ", purpose="Own draft", status=ClaimRequest.Status.DRAFT,
        )
        ClaimRequest.objects.create(
            requester=requester, accountant=user, work_location="HQ", purpose="Review actual",
            status=ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW,
        )
        ClaimRequest.objects.create(
            requester=requester, cfo=user, work_location="HQ", purpose="CFO acknowledgement",
            requested_total=Decimal("100.00"), approval_threshold=Decimal("5000.00"),
            status=ClaimRequest.Status.PENDING_PAYMENT,
        )
        ClaimRequest.objects.create(
            requester=requester, accountant=user, work_location="HQ", purpose="Completed by accountant",
            status=ClaimRequest.Status.COMPLETED,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard"))

        for card in response.context["status_cards"]:
            listing = self.client.get(card["url"])
            self.assertEqual(card["count"], listing.context["page_obj"].paginator.count, card["code"])

    def test_accountant_dashboard_counts_claims_they_completed(self):
        accountant = User.objects.create_user(username="dashboard-accountant", password="safe-test-password")
        accountant.signature.name = "signatures/test/dashboard-accountant.png"
        accountant.save(update_fields=["signature"])
        requester = User.objects.create_user(username="dashboard-requester", password="safe-test-password")
        supervisor = User.objects.create_user(username="dashboard-supervisor", password="safe-test-password")
        completed = ClaimRequest.objects.create(
            requester=requester, supervisor=supervisor, accountant=accountant,
            business_unit="IT", department="IT", work_location="HQ", budget="Office",
            purpose="Closed by accounting", status=ClaimRequest.Status.COMPLETED,
        )
        historical = ClaimRequest.objects.create(
            requester=requester, supervisor=supervisor, work_location="HQ",
            purpose="Previously reviewed", status=ClaimRequest.Status.COMPLETED,
        )
        ClaimApproval.objects.create(
            claim_request=historical, actor=accountant, action=ClaimApproval.Action.APPROVED,
            status_before=ClaimRequest.Status.PENDING_ACCOUNTING, status_after=ClaimRequest.Status.COMPLETED,
        )
        self.client.force_login(accountant)

        response = self.client.get(reverse("dashboard"))

        completed_card = next(card for card in response.context["status_cards"] if card["code"] == ClaimRequest.Status.COMPLETED)
        self.assertEqual(response.context["completed_count"], 1)
        self.assertEqual(completed_card["count"], 1)
        self.assertNotIn("mine=1", completed_card["url"])
        listing = self.client.get(completed_card["url"])
        self.assertEqual({claim.pk for claim in listing.context["claims"]}, {completed.pk})

    def test_accountant_dashboard_counts_claims_they_cancelled(self):
        accountant = User.objects.create_user(username="cancel-dashboard-accountant", password="safe-test-password")
        accountant.signature.name = "signatures/test/cancel-dashboard-accountant.png"
        accountant.save(update_fields=["signature"])
        requester = User.objects.create_user(username="cancel-dashboard-requester", password="safe-test-password")
        supervisor = User.objects.create_user(username="cancel-dashboard-supervisor", password="safe-test-password")
        claim = ClaimRequest.objects.create(
            requester=requester, supervisor=supervisor, accountant=accountant,
            business_unit="IT", department="IT", work_location="HQ", budget="Office",
            purpose="Cancelled by accounting", status=ClaimRequest.Status.CANCELLED,
        )
        ClaimApproval.objects.create(
            claim_request=claim, actor=accountant, action=ClaimApproval.Action.CANCELLED,
            status_before=ClaimRequest.Status.PENDING_CLEARANCE, status_after=ClaimRequest.Status.CANCELLED,
        )
        historical = ClaimRequest.objects.create(
            requester=requester, supervisor=supervisor, work_location="HQ",
            purpose="Previously reviewed cancellation", status=ClaimRequest.Status.CANCELLED,
        )
        ClaimApproval.objects.create(
            claim_request=historical, actor=accountant, action=ClaimApproval.Action.APPROVED,
            status_before=ClaimRequest.Status.PENDING_ACCOUNTING, status_after=ClaimRequest.Status.CANCELLED,
        )
        self.client.force_login(accountant)

        response = self.client.get(reverse("dashboard"))

        cancelled_card = next(card for card in response.context["status_cards"] if card["code"] == ClaimRequest.Status.CANCELLED)
        self.assertEqual(cancelled_card["count"], 1)
        self.assertNotIn("mine=1", cancelled_card["url"])
        listing = self.client.get(cancelled_card["url"])
        self.assertEqual({item.pk for item in listing.context["claims"]}, {claim.pk})


@override_settings(ALLOWED_HOSTS=["testserver"])
class TelegramWebhookCheckTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="webhook-admin", password="safe-test-password", telegram_user_id="99001")
        self.admin.signature.name = "signatures/test/webhook-admin.png"
        self.admin.save(update_fields=["signature"])
        self.client.force_login(self.admin)

    @patch("apps.core.views.send_telegram_message", return_value=True)
    @patch("apps.core.views.telegram_api_request", return_value=True)
    def test_admin_can_send_a_webhook_confirmation_button(self, telegram_api, send_message):
        response = self.client.post(reverse("admin_operations"), {"action": "telegram_webhook"})

        self.assertRedirects(response, reverse("admin_operations"))
        check = TelegramWebhookCheck.objects.get(requested_by=self.admin)
        self.assertIn(f"health:{check.token}", str(send_message.call_args.args))
        self.assertTrue(AdminAuditLog.objects.filter(actor=self.admin, action="TEST_TELEGRAM_WEBHOOK").exists())

    @patch("apps.claims.views.send_telegram_message", return_value=True)
    @patch("apps.claims.views.remove_telegram_callback_keyboard", return_value=True)
    @patch("apps.claims.views.answer_telegram_callback", return_value=True)
    def test_telegram_callback_confirms_webhook_check(self, answer_callback, remove_keyboard, send_message):
        check = TelegramWebhookCheck.objects.create(token="a" * 32, requested_by=self.admin)
        payload = {"callback_query": {"id": "health-callback", "data": f"health:{check.token}", "from": {"id": 99001}, "message": {"chat": {"id": 99001}, "message_id": 8}}}

        with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"):
            response = self.client.post(reverse("telegram_webhook"), data=json.dumps(payload), content_type="application/json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret")

        self.assertEqual(response.status_code, 200)
        check.refresh_from_db()
        self.assertIsNotNone(check.confirmed_at)
        answer_callback.assert_called_once_with("health-callback", "Webhook ใช้งานได้จริง ✅")
        remove_keyboard.assert_called_once_with(payload["callback_query"])
        self.assertTrue(AdminAuditLog.objects.filter(actor=self.admin, action="TEST_TELEGRAM_WEBHOOK").exists())


@override_settings(ALLOWED_HOSTS=["testserver"])
class NotificationFeedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="feed-user", password="safe-test-password")
        self.user.signature.name = "signatures/test/feed-user.png"
        self.user.save(update_fields=["signature"])
        self.client.force_login(self.user)

    def test_feed_returns_only_current_users_unread_notifications(self):
        other_user = User.objects.create_user(username="other-feed-user", password="safe-test-password")
        Notification.objects.create(recipient=self.user, title="New task", message="Review this request", link="/claims/1/")
        Notification.objects.create(recipient=self.user, title="Read", is_read=True)
        Notification.objects.create(recipient=other_user, title="Private")

        response = self.client.get(reverse("notification_feed"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["notifications"][0]["title"], "New task")

    def test_opening_one_notification_marks_only_that_notification_read(self):
        opened = Notification.objects.create(recipient=self.user, title="Open", link="/claims/1/")
        unread = Notification.objects.create(recipient=self.user, title="Keep", link="/claims/2/")

        response = self.client.get(reverse("notification_open", args=[opened.pk]))

        self.assertRedirects(response, "/claims/1/", fetch_redirect_response=False)
        opened.refresh_from_db()
        unread.refresh_from_db()
        self.assertTrue(opened.is_read)
        self.assertFalse(unread.is_read)
        self.assertEqual(self.client.get(reverse("notification_feed")).json()["count"], 1)

    def test_bell_page_keeps_notifications_unread_and_explicit_mark_all_clears_them(self):
        notification = Notification.objects.create(recipient=self.user, title="New task", link="/claims/1/")
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, f'href="{reverse("notification_open", args=[notification.pk])}" data-no-partial-navigation')
        notification.refresh_from_db()
        self.assertFalse(notification.is_read)
        self.assertEqual(self.client.get(reverse("notification_feed")).json()["count"], 1)

        response = self.client.post(reverse("mark_notifications_read"), {"next": reverse("dashboard")})
        self.assertRedirects(response, reverse("dashboard"))
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        self.assertEqual(self.client.get(reverse("notification_feed")).json()["count"], 0)


@override_settings(ALLOWED_HOSTS=["testserver"])
class SystemOverviewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="overview-admin", password="safe-test-password")
        self.requester = User.objects.create_user(username="overview-requester", password="safe-test-password")
        self.supervisor = User.objects.create_user(username="overview-supervisor", password="safe-test-password")
        for user in (self.admin, self.requester):
            user.signature.name = f"signatures/test/{user.username}.png"
            user.save(update_fields=["signature"])
        ClaimRequest.objects.create(requester=self.requester, supervisor=self.supervisor, business_unit="IT", department="IT", work_location="HQ", budget="Office", purpose="Monitor IT", requested_total=1000, status=ClaimRequest.Status.COMPLETED)
        ClaimRequest.objects.create(requester=self.requester, supervisor=self.supervisor, business_unit="Sales", department="Sales", work_location="HQ", budget="Office", purpose="Monitor Sales", requested_total=2000, status=ClaimRequest.Status.PENDING_SUPERVISOR)
        other_requester = User.objects.create_user(username="overview-other-requester", password="safe-test-password")
        ClaimRequest.objects.create(requester=other_requester, supervisor=self.supervisor, business_unit="WA", department="IT", work_location="HQ", budget="Office", purpose="Other requester", requested_total=500, status=ClaimRequest.Status.DRAFT)
        self.client.force_login(self.admin)

    def test_admin_can_filter_read_only_system_overview(self):
        response = self.client.get(reverse("system_overview"), {"requester": self.requester.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ภาพรวมระบบ")
        self.assertContains(response, "WEB HEALTH")
        self.assertContains(response, "CIAM")
        self.assertContains(response, 'data-monitor-search-select')
        self.assertContains(response, 'placeholder="พิมพ์ค้นหาผู้ขอ"')
        self.assertEqual(response.context["claim_count"], 2)
        self.assertEqual(response.context["completed_count"], 1)
        self.assertEqual(response.context["selected_requester"], str(self.requester.pk))
        self.assertIn("memory_current", response.context["web_health"])

    def test_non_admin_cannot_open_system_overview(self):
        self.client.force_login(self.requester)

        response = self.client.get(reverse("system_overview"))

        self.assertRedirects(response, reverse("dashboard"))

    def test_admin_can_search_audit_log_by_visible_thai_action(self):
        AdminAuditLog.objects.create(actor=self.admin, action="SUBMIT_CLAIM", target="BR-TEST")

        response = self.client.get(reverse("admin_operations"), {"audit_action": "SUBMIT_CLAIM", "audit_actor": "overview-admin"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ส่งคำขออนุมัติ")
        self.assertContains(response, 'value="SUBMIT_CLAIM"')
        self.assertContains(response, "ส่งคำขออนุมัติ")
        self.assertContains(response, 'data-search-select')
        self.assertContains(response, 'placeholder="พิมพ์ค้นหาเหตุการณ์"')
        self.assertContains(response, 'action="/settings/operations/#audit-log"')
