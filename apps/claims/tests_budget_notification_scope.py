from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.accounts.models import User
from .models import ClaimRequest
from .views import notify


@override_settings(TELEGRAM_BOT_TOKEN='')
class BudgetNotificationScopeTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username='scope-requester', department='IT')
        self.hr = User.objects.create_user(username='scope-hr', department='HR')
        self.finance = User.objects.create_user(username='scope-finance', department='Finance')
        self.claim = ClaimRequest.objects.create(
            requester=self.requester, purpose='Shared expenses', work_location='HQ',
            status=ClaimRequest.Status.PENDING_BUDGET_APPROVAL,
            requested_total=Decimal('350.00'), daily_requested_total=Decimal('900.00'),
            expense_month=date(2026, 10, 1),
        )
        for description, department, amount, approver in (
            ('IT postage', 'IT', '100', None),
            ('HR supplies', 'HR', '125', self.hr),
            ('HR training', 'HR', '75', self.hr),
            ('Finance supplies', 'Finance', '50', self.finance),
        ):
            self.claim.items.create(description=description, department=department,
                budget=f'{department} Budget', requested_amount=amount, budget_approver=approver)
        for manager in (self.hr, self.finance):
            self.claim.budget_approvals.create(department=manager.department, approver=manager)

    def message_for(self, recipient):
        with patch('apps.claims.views.send_telegram_notification') as bot, patch('apps.claims.views.send_telegram_group_notification') as group:
            notify(recipient, 'รออนุมัติ Budget', 'Please review', self.claim)
        message = bot.call_args.args[2]
        self.assertEqual(message, group.call_args.args[1])
        return message

    def test_hr_sees_only_its_two_lines_and_subtotal_in_bot_and_group(self):
        message = self.message_for(self.hr)
        self.assertIn('ยอดขออนุมัติ Budget (แผนก HR): 200.00 บาท', message)
        self.assertIn('HR supplies', message)
        self.assertIn('HR training', message)
        self.assertIn('ค่าใช้จ่ายของเดือน: ตุลาคม 2569', message)
        for text in ('IT postage', 'Finance supplies', '350.00', '900.00', 'ยอดขอสะสม'):
            self.assertNotIn(text, message)

    def test_other_budget_owner_gets_its_own_amount(self):
        message = self.message_for(self.finance)
        self.assertIn('ยอดขออนุมัติ Budget (แผนก Finance): 50.00 บาท', message)
        self.assertIn('Finance supplies', message)
        self.assertNotIn('HR supplies', message)
        self.assertNotIn('IT postage', message)

    def test_next_approval_stage_still_shows_whole_claim(self):
        self.claim.status = ClaimRequest.Status.PENDING_SUPERVISOR
        message = self.message_for(self.hr)
        self.assertIn('ยอดขอเบิก: 350.00 บาท', message)
        self.assertNotIn('ยอดขอสะสม', message)
        self.assertIn('รวมคำขอนี้: 900.00 บาท', message)
        for text in ('IT postage', 'HR supplies', 'HR training', 'Finance supplies'):
            self.assertIn(text, message)

    def test_same_department_different_assignee_is_not_included(self):
        self.claim.items.create(description='Other HR owner', department='HR',
            budget='Other HR Budget', requested_amount='90', budget_approver=self.finance)
        message = self.message_for(self.hr)
        self.assertNotIn('Other HR owner', message)
        self.assertIn('ยอดขออนุมัติ Budget (แผนก HR): 200.00 บาท', message)

    def test_daily_snapshot_only_shown_at_whole_claim_approval_stages(self):
        self.claim.daily_requested_total = Decimal('6170.00')
        self.claim.excess_daily_total = Decimal('6170.00')
        for revision in (0, 1):
            self.claim.amount_revision = revision
            for status in ClaimRequest.Status.values:
                self.claim.status = status
                message = self.message_for(self.hr)
                self.assertNotIn('ยอดขอสะสม', message)
                approval_stage = status in (ClaimRequest.Status.PENDING_SUPERVISOR,
                    ClaimRequest.Status.PENDING_EXECUTIVE, ClaimRequest.Status.PENDING_ACCOUNTING,
                    ClaimRequest.Status.PENDING_EXCESS_APPROVAL)
                if approval_stage:
                    self.assertIn('รวมคำขอนี้: 6,170.00 บาท', message)
                    self.assertIn('เกินเกณฑ์ 5,000.00 บาท ต้องให้ CFO อนุมัติ', message)
                elif status == ClaimRequest.Status.PENDING_PAYMENT:
                    self.assertIn('ยอดสะสมสำหรับอนุมัติ', message)
                    self.assertIn('รวมคำขอนี้: 6,170.00 บาท', message)
                    self.assertNotIn('ต้องให้ CFO อนุมัติ', message)
                else:
                    self.assertNotIn('6,170.00', message)
                self.assertEqual(self.claim.daily_requested_total, Decimal('6170.00'))
                self.assertTrue(self.claim.requires_cfo_approval)

    def test_excess_approval_uses_excess_snapshot_not_initial_and_exact_threshold_has_no_warning(self):
        self.claim.status = ClaimRequest.Status.PENDING_EXCESS_APPROVAL
        self.claim.daily_requested_total = Decimal('4500')
        self.claim.excess_daily_total = Decimal('5000')
        message = self.message_for(self.hr)
        self.assertIn('รวมคำขอนี้: 5,000.00 บาท', message)
        self.assertNotIn('4,500.00', message)
        self.assertNotIn('เกินเกณฑ์', message)

    def test_cfo_acknowledgement_does_not_show_daily_context(self):
        self.claim.status = ClaimRequest.Status.PENDING_ACCOUNTING
        with patch('apps.claims.views.send_telegram_notification') as bot, patch('apps.claims.views.send_telegram_group_notification'):
            notify(self.hr, 'รับทราบ', 'Please acknowledge', self.claim, cfo_ack_stage='initial')
        self.assertNotIn('รวมคำขอนี้', bot.call_args.args[2])
