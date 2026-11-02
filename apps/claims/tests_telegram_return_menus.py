import json
from uuid import uuid4
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.models import AdminAuditLog, Notification
from apps.core.telegram import replace_telegram_callback_keyboard
from .models import ClaimApproval, ClaimPayment, ClaimRequest
from .views import negative_approval_step, telegram_action_keyboard, telegram_rejection_keyboard, telegram_approval_callback_data


@override_settings(TELEGRAM_BOT_TOKEN='', TELEGRAM_WEBHOOK_SECRET='return-menu-secret')
class TelegramReturnMenuTests(TestCase):
    def setUp(self):
        names = ('requester', 'supervisor', 'executive', 'manager', 'cfo', 'accountant', 'budget', 'other')
        for index, name in enumerate(names):
            user = User.objects.create_user(username=f'menu-{name}', telegram_user_id=str(886600+index))
            user.signature.name = 'signatures/test/menu.png'
            user.save()
            setattr(self, name, user)
        self.api = patch('apps.core.telegram.telegram_api_request', return_value=True).start()
        self.send = patch('apps.claims.views.send_telegram_notification', return_value=True).start()
        self.group = patch('apps.claims.views.send_telegram_group_notification', return_value=True).start()
        self.addCleanup(patch.stopall)

    def make_claim(self, phase):
        fields = dict(requester=self.requester, supervisor=self.supervisor, executive=self.executive,
            accounting_manager=self.manager, cfo=self.cfo, accountant=self.accountant,
            request_type='ADVANCE', work_location='HQ', purpose='Menu navigation',
            requested_total=Decimal('100'), approval_threshold=Decimal('5000'))
        status, actor = {
            'budget': (ClaimRequest.Status.PENDING_BUDGET_APPROVAL, self.budget),
            'supervisor': (ClaimRequest.Status.PENDING_SUPERVISOR, self.supervisor),
            'executive': (ClaimRequest.Status.PENDING_EXECUTIVE, self.executive),
            'manager': (ClaimRequest.Status.PENDING_ACCOUNTING, self.manager),
            'cfo': (ClaimRequest.Status.PENDING_ACCOUNTING, self.cfo),
            'excess-manager': (ClaimRequest.Status.PENDING_EXCESS_APPROVAL, self.manager),
            'excess-cfo': (ClaimRequest.Status.PENDING_EXCESS_APPROVAL, self.cfo),
        }[phase]
        fields['status'] = status
        if phase == 'cfo':
            fields.update(requested_total=Decimal('6000'), accounting_approved_at=timezone.now())
        if phase.startswith('excess'):
            fields.update(actual_total=Decimal('5500'), accounting_approved_at=timezone.now(),
                excess_cfo_required=True, excess_daily_total=Decimal('5500'))
        if phase == 'excess-cfo':
            fields['excess_accounting_approved_at'] = timezone.now()
        claim = ClaimRequest.objects.create(**fields)
        claim.items.create(description='Expense', requested_amount=claim.requested_total, actual_amount=claim.actual_total)
        if phase == 'budget':
            claim.budget_approvals.create(department='HR', approver=self.budget)
        if phase.startswith('excess'):
            claim.payments.create(kind=ClaimPayment.Kind.INITIAL, amount='100',
                method=ClaimPayment.Method.CASH, paid_by=self.accountant)
            claim.approval_records.create(actor=self.accountant, action=ClaimApproval.Action.PAID,
                status_before=ClaimRequest.Status.PENDING_PAYMENT, status_after=ClaimRequest.Status.PAID)
            claim.attachments.create(file='claim-attachments/test.pdf', original_name='test.pdf', uploaded_by=self.requester)
        return claim, actor

    def callback(self, claim, actor, action):
        payload = {'callback_query': {'id': uuid4().hex, 'data': telegram_approval_callback_data(claim, action, actor=claim.current_approver() or actor),
            'from': {'id': int(actor.telegram_user_id)},
            'message': {'message_id': 9988, 'chat': {'id': int(actor.telegram_user_id)}}}}
        return self.client.post(reverse('telegram_webhook'), data=json.dumps(payload),
            content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='return-menu-secret')

    def test_every_approval_phase_has_return_and_both_reason_menus_have_back(self):
        for phase in ('budget', 'supervisor', 'executive', 'manager', 'cfo', 'excess-manager', 'excess-cfo'):
            claim, _ = self.make_claim(phase)
            actions = [button['callback_data'] for row in telegram_action_keyboard(claim)['inline_keyboard'] for button in row]
            for action in ('approve', 'reject-menu', 'return-menu'):
                self.assertIn(telegram_approval_callback_data(claim, action), actions, phase)
            for returning in (True, False):
                keyboard = telegram_rejection_keyboard(claim, returning=returning)
                self.assertEqual(keyboard['inline_keyboard'][-1][0]['callback_data'], telegram_approval_callback_data(claim, 'approval-menu'))

    def test_menu_and_back_only_replace_keyboard_no_state_or_notifications(self):
        for phase in ('budget', 'supervisor', 'executive', 'manager', 'cfo', 'excess-manager', 'excess-cfo'):
            claim, actor = self.make_claim(phase)
            original = claim.status
            original_records = claim.approval_records.count()
            for action in ('return-menu', 'approval-menu', 'reject-menu', 'approval-menu'):
                self.api.reset_mock()
                self.assertEqual(self.callback(claim, actor, action).status_code, 200)
                edits = [call for call in self.api.call_args_list if call.args[0] == 'editMessageReplyMarkup']
                self.assertEqual(len(edits), 1)
                self.assertEqual(edits[0].args[1]['message_id'], 9988)
                if action == 'approval-menu':
                    self.assertEqual(edits[0].args[1]['reply_markup'], telegram_action_keyboard(claim))
                claim.refresh_from_db()
                self.assertEqual(claim.status, original)
                self.assertEqual(claim.approval_records.count(), original_records)
            self.assertFalse(Notification.objects.filter(link=reverse('claim_detail', args=[claim.pk])).exists())
        self.assertFalse(AdminAuditLog.objects.filter(action='RETURN_CLAIM').exists())
        self.send.assert_not_called()
        self.group.assert_not_called()

    def test_confirm_return_works_at_all_initial_approval_phases_once(self):
        for phase in ('budget', 'supervisor', 'executive', 'manager', 'cfo'):
            claim, actor = self.make_claim(phase)
            self.callback(claim, actor, 'return-menu')
            self.callback(claim, actor, 'return-incomplete')
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.RETURNED)
            self.assertEqual(claim.approval_records.filter(action=ClaimApproval.Action.RETURNED).count(), 1)
            self.assertEqual(AdminAuditLog.objects.filter(action='RETURN_CLAIM', actor=actor).count(), 1)
            self.callback(claim, actor, 'approval-menu')
            self.callback(claim, actor, 'return-incomplete')
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.RETURNED)
            self.assertEqual(claim.approval_records.filter(action=ClaimApproval.Action.RETURNED).count(), 1)

    def test_telegram_approval_writes_central_audit_once_at_every_phase(self):
        for index, phase in enumerate(('budget', 'supervisor', 'executive', 'manager', 'cfo', 'excess-manager', 'excess-cfo')):
            with self.subTest(phase=phase):
                claim, actor = self.make_claim(phase)
                if index % 2:
                    claim.document_number = f'BR-AUDIT-{index}'
                    claim.save(update_fields=['document_number'])
                target = claim.document_number or f'คำขอ #{claim.pk}'
                self.assertEqual(self.callback(claim, actor, 'approve').status_code, 200)
                claim.refresh_from_db()
                audit = AdminAuditLog.objects.get(action='APPROVE_CLAIM', target=target)
                self.assertEqual(audit.actor, actor)
                self.assertEqual(audit.detail, '')
                record = claim.approval_records.get(action=ClaimApproval.Action.APPROVED)
                self.assertEqual(record.actor, actor)
                self.assertEqual(record.status_after, claim.status)
                self.assertEqual(self.callback(claim, actor, 'approve').status_code, 200)
                self.assertEqual(AdminAuditLog.objects.filter(action='APPROVE_CLAIM', target=target).count(), 1)
                self.assertEqual(claim.approval_records.filter(action=ClaimApproval.Action.APPROVED).count(), 1)

    def test_invalid_approval_and_menu_navigation_do_not_write_approval_audit(self):
        claim, actor = self.make_claim('manager')
        self.callback(claim, self.other, 'approve')
        for action in ('return-menu', 'approval-menu', 'reject-menu', 'approval-menu'):
            self.callback(claim, actor, action)
        self.assertFalse(AdminAuditLog.objects.filter(action='APPROVE_CLAIM').exists())
        self.assertFalse(claim.approval_records.exists())

    def test_back_then_approve_or_reject_remains_available(self):
        for next_action, expected in (('approve', ClaimRequest.Status.PENDING_ACCOUNTING),
                                      ('reject-budget', ClaimRequest.Status.REJECTED)):
            claim, actor = self.make_claim('supervisor')
            self.callback(claim, actor, 'return-menu')
            self.callback(claim, actor, 'approval-menu')
            self.callback(claim, actor, next_action)
            claim.refresh_from_db()
            self.assertEqual(claim.status, expected)
            self.assertFalse(claim.approval_records.filter(action=ClaimApproval.Action.RETURNED).exists())

    def test_unauthorized_actor_cannot_open_restore_or_return(self):
        claim, _ = self.make_claim('manager')
        for action in ('return-menu', 'approval-menu', 'return-budget'):
            self.api.reset_mock()
            self.callback(claim, self.other, action)
            self.assertFalse(any(call.args[0] == 'editMessageReplyMarkup' for call in self.api.call_args_list))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_ACCOUNTING)
        self.assertFalse(claim.approval_records.exists())

    def test_previous_actor_cannot_restore_menu_after_next_approval_stage(self):
        claim, actor = self.make_claim('manager')
        claim.requested_total = Decimal('6000')
        claim.save(update_fields=['requested_total'])
        self.callback(claim, actor, 'return-menu')
        self.callback(claim, actor, 'approval-menu')
        self.callback(claim, actor, 'approve')
        claim.refresh_from_db()
        self.assertEqual(claim.current_approver(), self.cfo)
        self.api.reset_mock()
        self.callback(claim, actor, 'approval-menu')
        self.assertFalse(any(call.args[0] == 'editMessageReplyMarkup' for call in self.api.call_args_list))
        self.callback(claim, actor, 'return-incomplete')
        claim.refresh_from_db()
        self.assertEqual(claim.current_approver(), self.cfo)

    def test_excess_return_preserves_initial_payment_and_restarts_actual_clearance(self):
        for phase in ('excess-manager', 'excess-cfo'):
            claim, actor = self.make_claim(phase)
            self.callback(claim, actor, 'return-incomplete')
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.PENDING_CLEARANCE)
            self.assertEqual(claim.payments.get().amount, Decimal('100'))
            self.assertEqual(claim.actual_total, Decimal('5500'))
            self.assertIsNotNone(claim.accounting_approved_at)
            self.assertIsNone(claim.excess_accounting_approved_at)
            self.assertIsNone(claim.excess_cfo_approved_at)
            self.client.force_login(self.requester)
            self.client.post(reverse('claim_submit', args=[claim.pk]))
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.PENDING_CLEARANCE)
            self.client.post(reverse('claim_clearance_submit', args=[claim.pk]), {f'actual_{claim.items.get().pk}': '5500'})
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.PENDING_EXCESS_APPROVAL)
            self.assertEqual(claim.current_approver(), self.manager)
            self.assertEqual(claim.payments.count(), 1)

    def test_web_excess_return_uses_same_safe_route(self):
        claim, actor = self.make_claim('excess-cfo')
        self.client.force_login(actor)
        self.client.post(reverse('claim_approval_action', args=[claim.pk]), {'action': 'return', 'comment': 'Check receipt'})
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_CLEARANCE)
        self.assertEqual(claim.payments.count(), 1)

    def test_negative_decision_rechecks_locked_actor_and_state(self):
        claim, actor = self.make_claim('manager')
        ClaimRequest.objects.filter(pk=claim.pk).update(accounting_approved_at=timezone.now())
        self.assertIsNone(negative_approval_step(claim, actor, returning=True, comment='Stale'))
        self.assertFalse(claim.approval_records.exists())

    def test_edit_keyboard_failure_sends_navigation_fallback_without_return(self):
        claim, actor = self.make_claim('supervisor')
        with patch('apps.claims.views.replace_telegram_callback_keyboard', return_value=False):
            self.callback(claim, actor, 'return-menu')
            self.assertEqual(self.send.call_args.kwargs['reply_markup'], telegram_rejection_keyboard(claim, returning=True))
            self.callback(claim, actor, 'approval-menu')
            self.assertEqual(self.send.call_args.kwargs['reply_markup'], telegram_action_keyboard(claim))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.assertFalse(claim.approval_records.exists())

    def test_core_keyboard_edit_requires_callback_message_metadata(self):
        self.assertFalse(replace_telegram_callback_keyboard({}, {'inline_keyboard': []}))
        self.api.assert_not_called()
