from datetime import date
import json
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.roles import EXECUTIVE_APPROVER, SUPERVISOR_APPROVER
from apps.core.models import Budget, BusinessUnit, Notification
from apps.core.system_settings import get_system_settings

from .forms import ClaimItemForm
from .models import ClaimApproval, ClaimBudgetApproval, ClaimPayment, ClaimRequest
from .views import telegram_approval_callback_data


@override_settings(TELEGRAM_BOT_TOKEN="")
class NewClaimWorkflowTests(TestCase):
    def setUp(self):
        # ตรึงวันที่เฉพาะชุดเดิมที่ทดสอบเรื่องอื่น ไม่ให้วันที่ตัวอย่างเก่าหมดอายุ
        self.policy_clock = patch('apps.claims.policies.current_date', return_value=date(2026, 9, 19))
        self.policy_clock.start()
        self.addCleanup(self.policy_clock.stop)
        self.requester = User.objects.create_user(username="claim-requester", password="test-pass", department="IT")
        BusinessUnit.objects.create(code="BU1", name="Test BU")
        Budget.objects.create(owner=self.requester, department="IT", name="Budget A")
        self.supervisor = User.objects.create_user(username="claim-supervisor", password="test-pass")
        self.supervisor.groups.add(Group.objects.get(name=SUPERVISOR_APPROVER))
        self.executive = User.objects.create_user(username="claim-executive", password="test-pass")
        self.manager = User.objects.create_user(username="claim-manager", password="test-pass")
        self.accountant = User.objects.create_user(username="claim-accountant", password="test-pass")
        self.cfo = User.objects.create_user(username="claim-cfo", password="test-pass")
        for user in (self.requester, self.supervisor, self.executive, self.manager, self.accountant, self.cfo):
            user.signature.name = f"signatures/test/{user.username}.png"
            user.save(update_fields=["signature"])
        config = get_system_settings()
        config.cfo = self.cfo
        config.save(update_fields=["cfo"])

    def make_claim(self, amount="100.00", request_type=ClaimRequest.RequestType.ADVANCE):
        claim = ClaimRequest.objects.create(
            requester=self.requester, supervisor=self.supervisor, executive=self.executive,
            accounting_manager=self.manager, accountant=self.accountant, cfo=self.cfo,
            request_type=request_type, expense_month=timezone.localdate().replace(day=1), work_location="HQ", purpose="ทดสอบ flow ใหม่",
        )
        claim.items.create(description="ค่าใช้จ่าย", business_unit="BU1", department="IT", budget="Budget A", requested_amount=amount)
        claim.refresh_requested_total()
        return claim

    def act(self, user, claim, action="approve"):
        self.client.force_login(user)
        response = self.client.post(reverse("claim_approval_action", args=[claim.pk]), {"action": action, "comment": "Wrong budget" if action == "reject" else ""})
        self.assertEqual(response.status_code, 302)
        claim.refresh_from_db()

    def test_other_department_budget_managers_approve_before_requester_approver(self):
        hr_manager = User.objects.create_user(
            username="hr-budget-manager", password="test-pass", department="HR",
            position_level=User.PositionLevel.MANAGER,
        )
        finance_manager = User.objects.create_user(
            username="finance-budget-manager", password="test-pass", department="Finance",
            position_level=User.PositionLevel.MANAGER,
        )
        for manager in (hr_manager, finance_manager):
            manager.signature.name = f"signatures/test/{manager.username}.png"
            manager.save(update_fields=["signature"])
        Budget.objects.create(owner=self.accountant, department="HR", name="HR Budget")
        Budget.objects.create(owner=self.accountant, department="Finance", name="Finance Budget")
        claim = self.make_claim()
        claim.items.create(description="HR item", business_unit="BU1", department="HR", budget="HR Budget",
                           budget_approver=hr_manager, requested_amount=100)
        claim.items.create(description="Finance item", business_unit="BU1", department="Finance",
                           budget="Finance Budget", budget_approver=finance_manager, requested_amount=100)
        claim.refresh_requested_total()

        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_BUDGET_APPROVAL)
        self.assertEqual(claim.budget_approvals.count(), 2)
        self.assertFalse(claim.can_approve(self.supervisor))
        self.assertEqual(Notification.objects.filter(recipient=self.supervisor).count(), 0)
        self.assertEqual(Notification.objects.filter(recipient=hr_manager).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=finance_manager).count(), 1)

        self.act(hr_manager, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_BUDGET_APPROVAL)
        self.assertFalse(claim.can_approve(hr_manager))
        self.assertTrue(claim.can_approve(finance_manager))
        self.assertEqual(Notification.objects.filter(recipient=finance_manager).count(), 1)
        self.act(finance_manager, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.assertEqual(claim.budget_approvals.filter(approved_at__isnull=False).count(), 2)
        self.assertEqual(Notification.objects.filter(recipient=self.supervisor).count(), 1)
        self.act(self.supervisor, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)

    def test_budget_rejection_returns_to_requester_and_resubmits(self):
        hr_manager = User.objects.create_user(
            username="hr-budget-manager", password="test-pass", department="HR",
            position_level=User.PositionLevel.MANAGER,
        )
        hr_manager.signature.name = "signatures/test/hr-budget-manager.png"
        hr_manager.save(update_fields=["signature"])
        Budget.objects.create(owner=self.accountant, department="HR", name="HR Budget")
        claim = self.make_claim()
        other_item = claim.items.create(description="HR item", business_unit="BU1", department="HR",
                                        budget="HR Budget", budget_approver=hr_manager, requested_amount=100)
        claim.refresh_requested_total()
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        self.act(hr_manager, claim, "reject")
        self.assertEqual(claim.status, ClaimRequest.Status.RETURNED)
        other_item.delete()
        claim.refresh_requested_total()
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.assertFalse(ClaimBudgetApproval.objects.filter(claim_request=claim).exists())

    def test_accounting_manager_returned_claim_restarts_requester_approval(self):
        claim = self.make_claim()
        item = claim.items.get()
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        self.act(self.supervisor, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)

        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("claim_approval_action", args=[claim.pk]),
            {"action": "return", "comment": "Please correct the amount"},
        )
        self.assertEqual(response.status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.RETURNED)
        self.assertTrue(Notification.objects.filter(recipient=self.requester, title__contains="ส่งกลับ").exists())

        self.client.force_login(self.requester)
        response = self.client.post(reverse("claim_edit", args=[claim.pk]), {
            "expense_month": "2026-10", "request_type": "ADVANCE", "request_date": "2026-10-19", "work_location": "HQ",
            "branch_name": "", "supervisor": self.supervisor.pk, "purpose": "Corrected claim",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "1",
            "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-id": item.pk, "items-0-claim_request": claim.pk,
            "items-0-description": "Corrected item", "items-0-business_unit": "BU1",
            "items-0-department": "IT", "items-0-budget": "Budget A",
            "items-0-requested_amount": "120.00",
        })
        self.assertEqual(response.status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.requested_total, Decimal("120.00"))
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.assertTrue(claim.can_approve(self.supervisor))
        self.assertFalse(claim.can_approve(self.manager))
        self.act(self.supervisor, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.assertTrue(claim.can_approve(self.manager))

    def test_accounting_return_rechecks_other_budget_before_requester_approval(self):
        hr_manager = User.objects.create_user(
            username="hr-budget-manager", password="test-pass", department="HR",
            position_level=User.PositionLevel.MANAGER,
        )
        hr_manager.signature.name = "signatures/test/hr-budget-manager.png"
        hr_manager.save(update_fields=["signature"])
        Budget.objects.create(owner=self.accountant, department="HR", name="HR Budget")
        claim = self.make_claim()
        claim.items.create(description="HR item", business_unit="BU1", department="HR", budget="HR Budget",
                           budget_approver=hr_manager, requested_amount=100)
        claim.refresh_requested_total()
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        self.act(hr_manager, claim)
        self.act(self.supervisor, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.client.force_login(self.manager)
        response = self.client.post(reverse("claim_approval_action", args=[claim.pk]), {
            "action": "return", "comment": "Please correct the request",
        })
        self.assertEqual(response.status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.RETURNED)
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_BUDGET_APPROVAL)
        self.assertTrue(claim.can_approve(hr_manager))
        self.assertFalse(claim.can_approve(self.supervisor))
        self.act(hr_manager, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)

    @patch("apps.claims.views.remove_telegram_callback_keyboard")
    @patch("apps.claims.views.answer_telegram_callback")
    @patch("apps.claims.views.send_telegram_notification")
    def test_accounting_manager_can_return_from_telegram(self, send_notification, answer_callback, remove_keyboard):
        from .views import telegram_action_keyboard

        claim = self.make_claim()
        claim.status = ClaimRequest.Status.PENDING_ACCOUNTING
        claim.save(update_fields=["status"])
        self.manager.telegram_user_id = "901235"
        self.manager.save(update_fields=["telegram_user_id"])
        keyboard = telegram_action_keyboard(claim)
        self.assertEqual(keyboard["inline_keyboard"][1][0]["callback_data"], telegram_approval_callback_data(claim, "return-menu"))

        def click(action):
            payload = {"callback_query": {"id": action, "data": telegram_approval_callback_data(claim, action), "from": {"id": 901235}}}
            with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"):
                return self.client.post(
                    reverse("telegram_webhook"), data=json.dumps(payload), content_type="application/json",
                    HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret",
                )

        self.assertEqual(click("return-menu").status_code, 200)
        self.assertEqual(send_notification.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         telegram_approval_callback_data(claim, "return-incomplete"))
        self.assertEqual(click("return-incomplete").status_code, 200)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.RETURNED)
        self.assertTrue(ClaimApproval.objects.filter(claim_request=claim, actor=self.manager, action=ClaimApproval.Action.RETURNED).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.requester, title__contains="ส่งกลับ").exists())
        self.assertEqual(remove_keyboard.call_count, 2)
        self.assertTrue(answer_callback.called)

    def test_transfer_requires_slip_and_records_payment_for_advance(self):
        claim = self.make_claim()
        claim.status = ClaimRequest.Status.PENDING_PAYMENT
        claim.save(update_fields=["status"])
        self.client.force_login(self.accountant)
        payment_url = reverse("claim_payment", args=[claim.pk])
        for payload in ({}, {"payment_method": "TRANSFER"}):
            self.assertEqual(self.client.post(payment_url, payload).status_code, 302)
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
            self.assertFalse(claim.payments.exists())

        with TemporaryDirectory() as media_dir, self.settings(MEDIA_ROOT=media_dir):
            response = self.client.post(payment_url, {
                "payment_method": "TRANSFER",
                "payment_slip": SimpleUploadedFile("transfer.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
            })
            self.assertEqual(response.status_code, 302)
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.PAID)
            payment = claim.payments.get()
            self.assertEqual(payment.kind, ClaimPayment.Kind.INITIAL)
            self.assertEqual(payment.method, ClaimPayment.Method.TRANSFER)
            self.assertEqual(payment.amount, Decimal("100.00"))
            self.assertEqual(payment.slip_original_name, "transfer.pdf")
            slip_url = reverse("claim_payment_slip_download", args=[claim.pk, payment.pk])
            self.assertContains(self.client.get(reverse("claim_detail", args=[claim.pk])), slip_url)
            self.assertEqual(self.client.get(slip_url).status_code, 200)
            self.client.force_login(self.supervisor)
            self.assertEqual(self.client.get(slip_url).status_code, 404)
            self.client.force_login(self.requester)
            self.assertEqual(self.client.get(slip_url).status_code, 200)

    def test_cash_payment_waits_for_petty_cash_receipt_without_slip(self):
        claim = self.make_claim(request_type=ClaimRequest.RequestType.PETTY_CASH)
        claim.status = ClaimRequest.Status.PENDING_PAYMENT
        claim.save(update_fields=["status"])
        self.client.force_login(self.accountant)
        response = self.client.post(reverse("claim_payment", args=[claim.pk]), {"payment_method": "CASH"})
        self.assertEqual(response.status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        payment = claim.payments.get()
        self.assertEqual(payment.method, ClaimPayment.Method.CASH)
        self.assertFalse(payment.slip)
        self.client.force_login(self.requester)
        detail = self.client.get(reverse("claim_detail", args=[claim.pk]))
        self.assertContains(detail, "ยืนยันรับเงิน")
        self.assertContains(detail, "ภายใน 3 วัน")
        self.client.post(reverse("claim_receipt_confirm", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)

    def test_cash_payment_rejects_unnecessary_slip(self):
        claim = self.make_claim()
        claim.status = ClaimRequest.Status.PENDING_PAYMENT
        claim.save(update_fields=["status"])
        self.client.force_login(self.accountant)
        self.client.post(reverse("claim_payment", args=[claim.pk]), {
            "payment_method": "CASH",
            "payment_slip": SimpleUploadedFile("cash.pdf", b"test", content_type="application/pdf"),
        })
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertFalse(claim.payments.exists())

    def test_item_rejects_budget_or_manager_from_wrong_department(self):
        hr_manager = User.objects.create_user(
            username="hr-budget-manager", password="test-pass", department="HR",
            position_level=User.PositionLevel.MANAGER,
        )
        finance_manager = User.objects.create_user(
            username="finance-budget-manager", password="test-pass", department="Finance",
            position_level=User.PositionLevel.MANAGER,
        )
        Budget.objects.create(owner=self.accountant, department="HR", name="HR Budget")
        base = {"description": "HR item", "business_unit": "BU1", "department": "HR",
                "budget": "HR Budget", "requested_amount": "100.00"}
        wrong_budget = ClaimItemForm(data={**base, "budget": "Budget A", "budget_approver": hr_manager.pk},
                                     requester=self.requester)
        self.assertFalse(wrong_budget.is_valid())
        self.assertIn("budget", wrong_budget.errors)
        wrong_manager = ClaimItemForm(data={**base, "budget_approver": finance_manager.pk},
                                      requester=self.requester)
        self.assertFalse(wrong_manager.is_valid())
        self.assertIn("budget_approver", wrong_manager.errors)
        valid = ClaimItemForm(data={**base, "budget_approver": hr_manager.pk}, requester=self.requester)
        self.assertTrue(valid.is_valid(), valid.errors)

    def test_small_advance_manager_approves_cfo_only_informed(self):
        claim = self.make_claim()
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.act(self.supervisor, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.assertFalse(claim.can_approve(self.cfo))
        self.assertFalse(claim.can_cfo_acknowledge(self.cfo, "initial"))
        self.assertFalse(Notification.objects.filter(recipient=self.cfo, title="CFO รับทราบคำขอ").exists())
        self.act(self.manager, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertTrue(claim.can_cfo_acknowledge(self.cfo, "initial"))
        self.assertEqual(Notification.objects.filter(recipient=self.cfo, title="CFO รับทราบคำขอ").count(), 1)

    def test_exactly_5000_still_goes_to_supervisor(self):
        claim = self.make_claim("5000.00")
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.assertTrue(claim.can_approve(self.supervisor))
        self.assertEqual(Notification.objects.filter(recipient=self.supervisor).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.executive).count(), 0)

    def test_cfo_web_acknowledgement_is_nonblocking_and_only_once(self):
        claim = self.make_claim()
        claim.status = ClaimRequest.Status.PENDING_ACCOUNTING
        claim.save(update_fields=["status"])
        self.client.force_login(self.cfo)
        self.assertNotContains(self.client.get(reverse("claim_detail", args=[claim.pk])), 'name="stage" value="initial"')
        self.client.post(reverse("claim_cfo_acknowledge", args=[claim.pk]), {"stage": "initial"})
        claim.refresh_from_db()
        self.assertIsNone(claim.cfo_acknowledged_at)
        self.act(self.manager, claim)
        self.client.force_login(self.cfo)
        detail = self.client.get(reverse("claim_detail", args=[claim.pk]))
        self.assertContains(detail, 'name="stage" value="initial"')
        self.assertContains(detail, ">รับทราบ</button>")
        self.assertContains(detail, 'data-confirm="ยืนยันรับทราบคำขอนี้?')
        self.assertNotContains(detail, 'name="action" value="approve"')
        with patch("apps.claims.views.send_telegram_group_notification") as group_notification:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("claim_cfo_acknowledge", args=[claim.pk]), {"stage": "initial"})
            self.assertEqual(group_notification.call_count, 1)
            self.assertIn("CFO รับทราบคำขอแล้ว", group_notification.call_args.args[0])
            self.assertIn(self.cfo.username, group_notification.call_args.args[1])
        self.assertEqual(response.status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertIsNotNone(claim.cfo_acknowledged_at)
        self.assertEqual(ClaimApproval.objects.filter(claim_request=claim, action=ClaimApproval.Action.ACKNOWLEDGED).count(), 1)
        with patch("apps.claims.views.send_telegram_group_notification") as group_notification:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("claim_cfo_acknowledge", args=[claim.pk]), {"stage": "initial"})
            group_notification.assert_not_called()
        self.assertEqual(ClaimApproval.objects.filter(claim_request=claim, action=ClaimApproval.Action.ACKNOWLEDGED).count(), 1)

    def test_cfo_acknowledgement_denied_when_approval_is_required(self):
        claim = self.make_claim("6000.00")
        claim.status = ClaimRequest.Status.PENDING_ACCOUNTING
        claim.save(update_fields=["status"])
        self.client.force_login(self.cfo)
        self.assertNotContains(self.client.get(reverse("claim_detail", args=[claim.pk])), 'name="stage" value="initial"')
        self.client.post(reverse("claim_cfo_acknowledge", args=[claim.pk]), {"stage": "initial"})
        claim.refresh_from_db()
        self.assertIsNone(claim.cfo_acknowledged_at)

    @patch("apps.claims.views.remove_telegram_callback_keyboard")
    @patch("apps.claims.views.answer_telegram_callback")
    def test_cfo_telegram_acknowledgement_has_one_button(self, answer_callback, remove_keyboard):
        claim = self.make_claim()
        claim.status = ClaimRequest.Status.PENDING_ACCOUNTING
        claim.save(update_fields=["status"])
        self.cfo.telegram_user_id = "901234"
        self.cfo.save(update_fields=["telegram_user_id"])
        payload = {"callback_query": {"id": "early-ack", "data": f"pc:{claim.pk}:ack-initial", "from": {"id": 901234}}}
        with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"):
            self.client.post(reverse("telegram_webhook"), data=json.dumps(payload), content_type="application/json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret")
        claim.refresh_from_db()
        self.assertIsNone(claim.cfo_acknowledged_at)
        self.act(self.manager, claim)
        with patch("apps.claims.views.send_telegram_notification") as sent, patch("apps.claims.views.send_telegram_group_notification"):
            from .views import notify
            notify(self.cfo, "CFO รับทราบคำขอ", "กดรับทราบ", claim, cfo_ack_stage="initial")
        self.assertEqual(sent.call_args.args[4], {"inline_keyboard": [[{"text": "รับทราบ", "callback_data": f"pc:{claim.pk}:ack-initial"}]]})
        payload = {"callback_query": {"id": "ack-callback", "data": f"pc:{claim.pk}:ack-initial", "from": {"id": 901234}}}
        with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"), patch("apps.claims.views.send_telegram_group_notification") as group_notification:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("telegram_webhook"), data=json.dumps(payload), content_type="application/json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret")
            group_notification.assert_called_once()
        self.assertEqual(response.status_code, 200)
        claim.refresh_from_db()
        self.assertIsNotNone(claim.cfo_acknowledged_at)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertEqual(remove_keyboard.call_count, 2)
        answer_callback.assert_called_with("ack-callback", "บันทึกรับทราบแล้ว")

    def test_excess_cfo_acknowledgement_waits_for_manager(self):
        claim = self.make_claim()
        claim.status = ClaimRequest.Status.PENDING_EXCESS_APPROVAL
        claim.actual_total = Decimal("125.00")
        claim.accounting_approved_at = timezone.now()
        claim.save(update_fields=["status", "actual_total", "accounting_approved_at"])
        self.assertFalse(claim.can_cfo_acknowledge(self.cfo, "excess"))
        self.client.force_login(self.cfo)
        self.assertNotContains(self.client.get(reverse("claim_detail", args=[claim.pk])), 'name="stage" value="excess"')
        self.client.post(reverse("claim_cfo_acknowledge", args=[claim.pk]), {"stage": "excess"})
        claim.refresh_from_db()
        self.assertIsNone(claim.cfo_excess_acknowledged_at)
        self.assertFalse(Notification.objects.filter(recipient=self.cfo, title="CFO รับทราบยอดใช้เกิน").exists())
        self.act(self.manager, claim)
        self.assertTrue(claim.can_cfo_acknowledge(self.cfo, "excess"))
        self.assertEqual(Notification.objects.filter(recipient=self.cfo, title="CFO รับทราบยอดใช้เกิน").count(), 1)
        self.client.force_login(self.cfo)
        self.assertContains(self.client.get(reverse("claim_detail", args=[claim.pk])), 'name="stage" value="excess"')

    def test_claim_detail_shows_assigned_accountant(self):
        claim = self.make_claim()
        self.client.force_login(self.requester)
        response = self.client.get(reverse("claim_detail", args=[claim.pk]))
        self.assertContains(response, "เจ้าหน้าที่บัญชี")
        self.assertContains(response, self.accountant.username)

    def test_detail_pairs_executive_with_accounting_manager_without_empty_row(self):
        claim = self.make_claim("6000.00")
        self.client.force_login(self.requester)
        response = self.client.get(reverse("claim_detail", args=[claim.pk]))
        self.assertNotContains(response, 'class="detail-wide"')
        html = response.content.decode()
        labels = ("<dt>ประเภท</dt>", "<dt>ผู้ขอเบิก</dt>", "<dt>ผู้อนุมัติ</dt>", "<dt>ผู้จัดการบัญชี</dt>", "<dt>CFO</dt>", "<dt>เจ้าหน้าที่บัญชี</dt>", "<dt>สถานที่</dt>", "<dt>วัตถุประสงค์</dt>")
        self.assertEqual([html.index(label) for label in labels], sorted(html.index(label) for label in labels))

    def test_print_form_omits_extra_approval_note(self):
        claim = self.make_claim()
        claim.status = ClaimRequest.Status.COMPLETED
        claim.save(update_fields=["status"])
        self.client.force_login(self.requester)
        response = self.client.get(reverse("claim_print", args=[claim.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "อนุมัติเพิ่มเติมตาม flow")
        self.assertNotContains(response, "CFO รับทราบ:")

    def test_print_setting_hides_button_and_blocks_direct_print_url(self):
        claim = self.make_claim()
        claim.status = ClaimRequest.Status.COMPLETED
        claim.save(update_fields=["status"])
        self.client.force_login(self.accountant)
        detail_url = reverse("claim_detail", args=[claim.pk])
        print_url = reverse("claim_print", args=[claim.pk])
        self.assertContains(self.client.get(detail_url), "เปิดเอกสารสำหรับพิมพ์")
        self.assertEqual(self.client.get(print_url).status_code, 200)

        configuration = get_system_settings()
        configuration.claim_print_enabled = False
        configuration.save(update_fields=["claim_print_enabled"])
        self.assertNotContains(self.client.get(detail_url), "เปิดเอกสารสำหรับพิมพ์")
        self.assertEqual(self.client.get(print_url).status_code, 404)

    def test_high_advance_needs_assigned_approver_manager_and_cfo(self):
        claim = self.make_claim("5000.01")
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.assertEqual(claim.current_approver(), self.supervisor)
        self.assertFalse(claim.can_approve(self.executive))
        self.assertEqual(Notification.objects.filter(recipient=self.supervisor).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.executive).count(), 0)
        self.client.force_login(self.executive)
        self.assertEqual(self.client.post(reverse("claim_approval_action", args=[claim.pk]), {"action": "approve"}).status_code, 404)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.act(self.supervisor, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.assertFalse(claim.can_approve(self.cfo))
        self.assertEqual(Notification.objects.filter(recipient=self.manager).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.cfo).count(), 0)
        self.client.force_login(self.cfo)
        self.assertNotContains(self.client.get(reverse("approval_queue")), claim.purpose)
        with patch("apps.claims.views.send_telegram_notification") as direct_notification:
            self.act(self.manager, claim)
        self.assertEqual(direct_notification.call_args.args[0], self.cfo)
        self.assertEqual(direct_notification.call_args.args[4]["inline_keyboard"][0][0]["callback_data"], telegram_approval_callback_data(claim, "approve"))
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.assertTrue(claim.can_approve(self.cfo))
        self.assertEqual(claim.workflow_status_display, "รอ CFO อนุมัติ")
        self.assertEqual(Notification.objects.filter(recipient=self.cfo).count(), 1)
        self.client.force_login(self.cfo)
        self.assertContains(self.client.get(reverse("approval_queue")), claim.purpose)
        self.act(self.cfo, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)

    def test_manager_requester_always_needs_cfo(self):
        self.requester.position_level = User.PositionLevel.MANAGER
        self.requester.save(update_fields=["position_level"])
        claim = self.make_claim()
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        self.act(self.supervisor, claim)
        self.assertFalse(claim.can_approve(self.cfo))
        self.assertEqual(Notification.objects.filter(recipient=self.cfo).count(), 0)
        self.act(self.manager, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.assertEqual(Notification.objects.filter(recipient=self.cfo).count(), 1)
        self.act(self.cfo, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)

    def test_cfo_as_old_executive_only_approves_after_manager(self):
        claim = self.make_claim("6000.00")
        claim.executive = self.cfo
        claim.save(update_fields=["executive"])
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        self.assertFalse(claim.can_approve(self.cfo))
        self.act(self.supervisor, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.assertIsNone(claim.cfo_approved_at)
        self.act(self.manager, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.act(self.cfo, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)

    def test_cfo_cannot_approve_before_accounting_manager(self):
        claim = self.make_claim("6000.00")
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        self.act(self.supervisor, claim)
        self.client.force_login(self.cfo)
        self.assertNotContains(self.client.get(reverse("claim_detail", args=[claim.pk])), 'name="action" value="approve"')
        self.assertEqual(self.client.post(reverse("claim_approval_action", args=[claim.pk]), {"action": "approve"}).status_code, 302)
        claim.refresh_from_db()
        self.assertIsNone(claim.cfo_approved_at)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.act(self.manager, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.act(self.cfo, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)

    @patch("apps.claims.views.answer_telegram_callback")
    def test_old_cfo_telegram_button_cannot_approve_before_manager(self, answer_callback):
        claim = self.make_claim("6000.00")
        claim.status = ClaimRequest.Status.PENDING_ACCOUNTING
        claim.save(update_fields=["status"])
        self.cfo.telegram_user_id = "901235"
        self.cfo.save(update_fields=["telegram_user_id"])
        payload = {"callback_query": {"id": "early-cfo", "data": f"pc:{claim.pk}:approve", "from": {"id": 901235}}}
        with self.settings(TELEGRAM_WEBHOOK_SECRET="webhook-test-secret"):
            response = self.client.post(reverse("telegram_webhook"), data=json.dumps(payload), content_type="application/json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret")
        self.assertEqual(response.status_code, 200)
        claim.refresh_from_db()
        self.assertIsNone(claim.cfo_approved_at)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        answer_callback.assert_called_once_with("early-cfo", "ปุ่มอนุมัติรุ่นเก่าหมดอายุ กรุณาเปิดหน้าเว็บเพื่อทำรายการ")

    def test_advance_requester_records_actual_then_accountant_closes(self):
        claim = self.make_claim()
        claim.status = ClaimRequest.Status.PENDING_CLEARANCE
        claim.save(update_fields=["status"])
        item = claim.items.get()
        self.client.force_login(self.requester)
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(reverse("claim_clearance_submit", args=[claim.pk]), {
                f"actual_{item.pk}": "80.00",
                "attachments": SimpleUploadedFile("receipt.pdf", b"receipt", content_type="application/pdf"),
            })
        self.assertEqual(response.status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW)
        self.assertEqual(claim.actual_total, Decimal("80.00"))
        self.client.force_login(self.accountant)
        self.client.post(reverse("claim_complete", args=[claim.pk]), {"refund_confirmed": "1"})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)

    def test_accountant_corrects_actual_before_closing_with_audit_trail(self):
        claim = self.make_claim()
        item = claim.items.get()
        item.actual_amount = Decimal("80.00")
        item.save(update_fields=["actual_amount"])
        claim.actual_total = Decimal("80.00")
        claim.status = ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW
        claim.save(update_fields=["actual_total", "status"])
        url = reverse("claim_clearance_correct", args=[claim.pk])

        self.client.force_login(self.requester)
        self.assertEqual(self.client.post(url, {f"corrected_actual_{item.pk}": "85.00"}).status_code, 403)
        self.client.force_login(self.accountant)
        self.assertContains(self.client.get(reverse("claim_detail", args=[claim.pk])), "แก้ยอดใช้จริง")
        self.client.post(url, {f"corrected_actual_{item.pk}": "85.00", "correction_note": "ตรวจใบเสร็จแล้ว"})
        claim.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(claim.actual_total, Decimal("85.00"))
        self.assertEqual(item.actual_amount, Decimal("85.00"))
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW)
        correction = claim.approval_records.get(action=ClaimApproval.Action.ACTUAL_CORRECTED)
        self.assertIn("80.00 → 85.00", correction.comment)
        self.assertIn("ตรวจใบเสร็จแล้ว", correction.comment)
        self.client.post(reverse("claim_complete", args=[claim.pk]), {"refund_confirmed": "1"})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)
        self.assertEqual(claim.approval_records.filter(action=ClaimApproval.Action.ACTUAL_CORRECTED).count(), 1)

    def test_accountant_correction_of_approved_excess_updates_supplemental_payment(self):
        claim = self.make_claim()
        item = claim.items.get()
        item.actual_amount = Decimal("125.00")
        item.save(update_fields=["actual_amount"])
        claim.actual_total = Decimal("125.00")
        claim.status = ClaimRequest.Status.PENDING_PAYMENT
        claim.save(update_fields=["actual_total", "status"])
        self.client.force_login(self.accountant)
        self.client.post(reverse("claim_clearance_correct", args=[claim.pk]), {
            f"corrected_actual_{item.pk}": "120.00",
        })
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertEqual(claim.settlement_amount, Decimal("20.00"))
        self.client.post(reverse("claim_payment", args=[claim.pk]), {"payment_method": "CASH"})
        payment = claim.payments.get(kind=ClaimPayment.Kind.SUPPLEMENTAL)
        self.assertEqual(payment.amount, Decimal("20.00"))
        self.client.post(reverse("claim_clearance_correct", args=[claim.pk]), {
            f"corrected_actual_{item.pk}": "130.00",
        })
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.assertEqual(claim.actual_total, Decimal("120.00"))

    def test_accountant_correction_that_creates_first_excess_needs_approval(self):
        claim = self.make_claim()
        item = claim.items.get()
        item.actual_amount = Decimal("80.00")
        item.save(update_fields=["actual_amount"])
        claim.actual_total = Decimal("80.00")
        claim.status = ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW
        claim.save(update_fields=["actual_total", "status"])
        self.client.force_login(self.accountant)
        self.client.post(reverse("claim_clearance_correct", args=[claim.pk]), {
            f"corrected_actual_{item.pk}": "105.00",
        })
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_EXCESS_APPROVAL)
        self.assertEqual(claim.settlement_amount, Decimal("5.00"))
        self.assertTrue(claim.can_approve(self.manager))

    def test_accountant_correction_removes_excess_and_prevents_extra_payment(self):
        claim = self.make_claim()
        item = claim.items.get()
        item.actual_amount = Decimal("125.00")
        item.save(update_fields=["actual_amount"])
        claim.actual_total = Decimal("125.00")
        claim.status = ClaimRequest.Status.PENDING_PAYMENT
        claim.save(update_fields=["actual_total", "status"])
        self.client.force_login(self.accountant)
        self.client.post(reverse("claim_clearance_correct", args=[claim.pk]), {
            f"corrected_actual_{item.pk}": "90.00",
        })
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW)
        self.assertEqual(claim.settlement_amount, Decimal("10.00"))
        self.assertFalse(claim.has_excess)

    def test_petty_cash_needs_document_and_requester_receipt_before_closing(self):
        claim = self.make_claim(request_type=ClaimRequest.RequestType.PETTY_CASH)
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.DRAFT)
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            claim.attachments.create(
                file=SimpleUploadedFile("receipt.pdf", b"receipt"), original_name="receipt.pdf", uploaded_by=self.requester,
            )
            self.client.post(reverse("claim_submit", args=[claim.pk]))
            self.act(self.supervisor, claim)
            self.act(self.manager, claim)
            self.client.force_login(self.accountant)
            self.client.post(reverse("claim_payment", args=[claim.pk]), {"payment_method": "CASH"})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
        self.client.force_login(self.requester)
        self.client.post(reverse("claim_receipt_confirm", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.COMPLETED)

    def test_high_petty_cash_with_attachment_starts_at_assigned_approver(self):
        claim = self.make_claim("5000.01", request_type=ClaimRequest.RequestType.PETTY_CASH)
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            claim.attachments.create(
                file=SimpleUploadedFile("receipt.pdf", b"receipt"), original_name="receipt.pdf", uploaded_by=self.requester,
            )
            self.client.force_login(self.requester)
            self.client.post(reverse("claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.assertEqual(Notification.objects.filter(recipient=self.supervisor).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.executive).count(), 0)

    def test_form_requires_assigned_approver_for_both_high_and_low_amounts(self):
        self.requester.accounting_manager = self.manager
        self.requester.accountant = self.accountant
        self.requester.save(update_fields=["accounting_manager", "accountant"])
        payload = {
            "expense_month": "2026-10", "request_type": "ADVANCE", "request_date": "06/10/2026", "work_location": "HQ",
            "purpose": "ทดสอบผู้อนุมัติที่ผูกไว้",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "ซื้ออุปกรณ์", "items-0-business_unit": "BU1", "items-0-department": "IT",
            "items-0-budget": "Budget A", "items-0-requested_amount": "5000.01",
        }
        self.client.force_login(self.requester)
        response = self.client.post(reverse("claim_create"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ClaimRequest.objects.count(), 0)
        response = self.client.post(reverse("claim_create"), {**payload, "supervisor": str(self.supervisor.pk)})
        self.assertEqual(response.status_code, 302)
        claim = ClaimRequest.objects.get()
        self.assertEqual(claim.supervisor, self.supervisor)
        self.client.post(reverse("claim_submit", args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.assertContains(self.client.get(reverse("claim_detail", args=[claim.pk])), "ผู้อนุมัติ")

        low_response = self.client.post(reverse("claim_create"), {**payload, "items-0-requested_amount": "5000.00"})
        self.assertEqual(low_response.status_code, 200)
        self.assertEqual(ClaimRequest.objects.count(), 1)

    def test_out_of_pocket_form_requires_attachment_before_saving_draft(self):
        payload = {
            "expense_month": "2026-10", "request_type": "PETTY_CASH", "request_date": "06/10/2026", "work_location": "HQ",
            "supervisor": str(self.supervisor.pk), "purpose": "ออกเงินไปก่อน",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "ซื้ออุปกรณ์", "items-0-business_unit": "BU1", "items-0-department": "IT",
            "items-0-budget": "Budget A", "items-0-requested_amount": "100.00",
        }
        self.client.force_login(self.requester)
        response = self.client.post(reverse("claim_create"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ต้องแนบเอกสารประกอบก่อนบันทึกคำขอ")
        self.assertFalse(ClaimRequest.objects.exists())

        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(reverse("claim_create"), {
                **payload, "attachments": SimpleUploadedFile("receipt.pdf", b"receipt", content_type="application/pdf"),
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ClaimRequest.objects.count(), 1)

    def test_editing_out_of_pocket_draft_without_attachment_shows_error(self):
        claim = self.make_claim(request_type=ClaimRequest.RequestType.PETTY_CASH)
        item = claim.items.get()
        payload = {
            "expense_month": "2026-10", "request_type": "PETTY_CASH", "request_date": "06/10/2026", "work_location": "HQ",
            "supervisor": str(self.supervisor.pk), "purpose": "แก้ไขแบบร่าง",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "1", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-id": str(item.pk), "items-0-description": item.description,
            "items-0-business_unit": "BU1", "items-0-department": "IT", "items-0-budget": "Budget A",
            "items-0-requested_amount": "100.00",
        }
        self.client.force_login(self.requester)
        response = self.client.post(reverse("claim_edit", args=[claim.pk]), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ต้องแนบเอกสารประกอบก่อนบันทึกคำขอ")

        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            attachment = claim.attachments.create(
                file=SimpleUploadedFile("receipt.pdf", b"receipt", content_type="application/pdf"),
                original_name="receipt.pdf", uploaded_by=self.requester,
            )
            edit_response = self.client.get(reverse("claim_edit", args=[claim.pk]))
            html = edit_response.content.decode()
            form_start = html.index('<form method="post" class="claim-form"')
            form_end = html.index('</form>', form_start)
            self.assertIn('class="button button-primary claim-save"', html[form_start:form_end])
            self.assertIn(f'form="attachment-delete-{attachment.pk}"', html[form_start:form_end])
            response = self.client.post(reverse("claim_edit", args=[claim.pk]), payload)
        self.assertEqual(response.status_code, 302)

    def test_clearance_without_any_attachment_is_rejected(self):
        claim = self.make_claim()
        claim.status = ClaimRequest.Status.PENDING_CLEARANCE
        claim.save(update_fields=["status"])
        item = claim.items.get()
        self.client.force_login(self.requester)
        response = self.client.get(reverse("claim_detail", args=[claim.pk]))
        self.assertContains(response, 'id="clearance-attachments"')
        self.assertContains(response, 'class="button button-primary clearance-submit-button"')
        self.assertNotContains(response, 'name="clearance_note"')
        self.assertNotContains(response, f'name="actual_description_{item.pk}"')
        self.client.post(reverse("claim_clearance_submit", args=[claim.pk]), {f"actual_{item.pk}": "80.00"})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_CLEARANCE)
        self.assertIsNone(claim.actual_total)

    def test_excess_advance_manager_approval_cfo_only_informed(self):
        claim = self.make_claim()
        claim.status = ClaimRequest.Status.PENDING_CLEARANCE
        claim.save(update_fields=["status"])
        item = claim.items.get()
        self.client.force_login(self.requester)
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            claim.attachments.create(
                file=SimpleUploadedFile("original.pdf", b"receipt", content_type="application/pdf"),
                original_name="original.pdf", uploaded_by=self.requester,
            )
            self.client.post(reverse("claim_clearance_submit", args=[claim.pk]), {
                f"actual_{item.pk}": "125.00",
            })
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_EXCESS_APPROVAL)
        self.assertEqual(claim.excess_reason, "")
        self.assertFalse(claim.can_approve(self.cfo))
        self.act(self.manager, claim)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.client.force_login(self.accountant)
        self.client.post(reverse("claim_payment", args=[claim.pk]), {"payment_method": "CASH"})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PAID)
