import json
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import connections
from django.contrib.auth.models import Group
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from apps.core.models import AdminAuditLog, Notification
from apps.accounts.models import User
from apps.accounts.roles import ROLE_NAMES
from .models import ClaimApproval, ClaimRequest, TelegramApprovalPrompt, TelegramCallbackReceipt
from .tests_telegram_return_menus import TelegramReturnMenuTests
from .views import telegram_approval_callback_data, telegram_action_keyboard, telegram_rejection_keyboard


@override_settings(TELEGRAM_BOT_TOKEN='', TELEGRAM_WEBHOOK_SECRET='return-menu-secret')
class TelegramApprovalSafetyTests(TestCase):
    setUp = TelegramReturnMenuTests.setUp
    make_claim = TelegramReturnMenuTests.make_claim

    def share_chat(self):
        for actor in (self.supervisor, self.manager, self.cfo):
            actor.telegram_user_id = '886699'
            actor.save(update_fields=['telegram_user_id'])

    def click(self, data, actor, callback_id=None):
        payload = {'callback_query': {'id': callback_id or uuid4().hex, 'data': data,
            'from': {'id': int(actor.telegram_user_id)},
            'message': {'message_id': 9988, 'chat': {'id': int(actor.telegram_user_id)}}}}
        return self.client.post(reverse('telegram_webhook'), data=json.dumps(payload),
            content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='return-menu-secret')

    def scoped(self, claim, actor, action='approve'):
        return telegram_approval_callback_data(claim, action, actor=actor)

    def test_shared_chat_needs_three_separate_buttons_supervisor_manager_then_cfo(self):
        self.share_chat()
        claim, _ = self.make_claim('supervisor')
        claim.requested_total = Decimal('6000')
        claim.save(update_fields=['requested_total'])
        head = self.scoped(claim, self.supervisor)
        self.click(head, self.supervisor, 'head-click')
        claim.refresh_from_db()
        self.assertEqual(claim.current_approver(), self.manager)
        self.assertFalse(Notification.objects.filter(recipient=self.cfo).exists())
        for callback_id in ('head-click', 'head-double-click'):
            self.click(head, self.supervisor, callback_id)
        claim.refresh_from_db()
        self.assertIsNone(claim.accounting_approved_at)
        manager = self.scoped(claim, self.manager)
        self.click(manager, self.manager, 'manager-click')
        claim.refresh_from_db()
        self.assertEqual(claim.current_approver(), self.cfo)
        for callback_id in ('manager-click', 'manager-double-click'):
            self.click(manager, self.manager, callback_id)
        claim.refresh_from_db()
        self.assertIsNone(claim.cfo_approved_at)
        self.assertFalse(Notification.objects.filter(recipient=self.accountant).exists())
        self.click(self.scoped(claim, self.cfo), self.cfo)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertEqual(list(claim.approval_records.filter(action='APPROVED').values_list('actor_id', flat=True)),
            [self.supervisor.pk, self.manager.pk, self.cfo.pk])
        self.assertEqual(Notification.objects.filter(recipient=self.accountant, title='รอเจ้าหน้าที่บัญชีจ่ายเงิน').count(), 1)

    def test_shared_chat_excess_duplicate_manager_callback_cannot_act_as_cfo(self):
        self.share_chat()
        claim, _ = self.make_claim('excess-manager')
        data = self.scoped(claim, self.manager)
        self.click(data, self.manager, 'excess-click')
        for callback_id in ('excess-click', 'second-click'):
            self.click(data, self.manager, callback_id)
        claim.refresh_from_db()
        self.assertEqual(claim.current_approver(), self.cfo)
        self.assertIsNone(claim.excess_cfo_approved_at)
        self.assertEqual(claim.approval_records.filter(action='APPROVED').count(), 1)
        self.assertEqual(AdminAuditLog.objects.filter(action='APPROVE_CLAIM').count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.cfo, title='รอ CFO อนุมัติส่วนต่าง').count(), 1)
        self.assertFalse(Notification.objects.filter(recipient=self.accountant).exists())
        self.click(self.scoped(claim, self.cfo), self.cfo)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertEqual(claim.payments.count(), 1)  # No supplemental payment created by approval.

    def test_manager_token_cannot_be_used_by_another_telegram_user(self):
        claim, _ = self.make_claim('manager')
        self.click(self.scoped(claim, self.manager), self.other)
        claim.refresh_from_db()
        self.assertIsNone(claim.accounting_approved_at)
        self.assertFalse(claim.approval_records.exists())

    def test_legacy_approval_is_retired_even_with_a_shared_chat(self):
        self.share_chat()
        claim, _ = self.make_claim('excess-manager')
        for action in ('approve', 'reject-budget', 'return-incomplete', 'approval-menu'):
            self.click(f'pc:{claim.pk}:{action}', self.manager)
        claim.refresh_from_db()
        self.assertIsNone(claim.excess_accounting_approved_at)
        self.assertFalse(claim.approval_records.filter(action='APPROVED').exists())
        self.assertFalse(Notification.objects.exists())

    def test_old_round_token_is_invalid_after_return_and_resubmission_to_same_actor(self):
        claim, _ = self.make_claim('manager')
        old = self.scoped(claim, self.manager)
        claim.approval_records.create(actor=self.requester, action='SUBMITTED',
            status_before='RETURNED', status_after='PENDING_SUPERVISOR')
        self.click(old, self.manager)
        claim.refresh_from_db()
        self.assertIsNone(claim.accounting_approved_at)
        fresh = self.scoped(claim, self.manager)
        self.assertNotEqual(old, fresh)
        self.click(fresh, self.manager)
        claim.refresh_from_db()
        self.assertIsNotNone(claim.accounting_approved_at)

    def test_old_menu_reason_and_back_cannot_cross_to_cfo_in_shared_chat(self):
        self.share_chat()
        claim, _ = self.make_claim('excess-manager')
        old_actions = [self.scoped(claim, self.manager, action) for action in
            ('return-menu', 'return-incomplete', 'reject-menu', 'reject-budget', 'approval-menu')]
        self.click(self.scoped(claim, self.manager), self.manager)
        for data in old_actions:
            self.click(data, self.manager)
        claim.refresh_from_db()
        self.assertEqual(claim.current_approver(), self.cfo)
        self.assertFalse(claim.approval_records.filter(action__in=['RETURNED', 'REJECTED']).exists())

    def test_multiple_external_budget_owners_can_use_independent_prompts(self):
        claim, _ = self.make_claim('budget')
        claim.budget_approvals.create(department='Finance', approver=self.other)
        first = self.scoped(claim, self.budget)
        second = self.scoped(claim, self.other)
        self.click(first, self.budget)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_BUDGET_APPROVAL)
        self.click(second, self.other)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        self.assertEqual(claim.budget_approvals.filter(approved_at__isnull=False).count(), 2)

    def test_menu_back_and_reasons_preserve_same_actor_scope_and_fit_64_bytes(self):
        claim, _ = self.make_claim('manager')
        keyboard = telegram_action_keyboard(claim, actor=self.manager)
        for returning in (False, True):
            keyboard['inline_keyboard'] += telegram_rejection_keyboard(claim, returning=returning, actor=self.manager)['inline_keyboard']
        for row in keyboard['inline_keyboard']:
            for button in row:
                data = button['callback_data']
                self.assertLessEqual(len(data.encode()), 64)
                self.assertTrue(data.startswith('pa:'))
        self.assertEqual(TelegramApprovalPrompt.objects.filter(claim_request=claim).count(), 1)

    def test_callback_receipt_rolls_back_when_processing_fails_then_retry_can_succeed(self):
        claim, _ = self.make_claim('manager')
        data = self.scoped(claim, self.manager)
        with patch('apps.claims.views.process_telegram_approval', side_effect=RuntimeError('temporary failure')):
            with self.assertRaises(RuntimeError):
                self.click(data, self.manager, 'retryable')
        self.assertFalse(TelegramCallbackReceipt.objects.filter(callback_id='retryable').exists())
        self.click(data, self.manager, 'retryable')
        claim.refresh_from_db()
        self.assertIsNotNone(claim.accounting_approved_at)
        self.assertEqual(TelegramCallbackReceipt.objects.filter(callback_id='retryable').count(), 1)

    def test_malformed_or_unknown_prompt_never_changes_approval(self):
        claim, _ = self.make_claim('manager')
        for data in ('pa:not-a-uuid:approve', f'pa:{uuid4().hex}:approve', self.scoped(claim, self.manager).replace(':approve', ':receive')):
            self.assertEqual(self.click(data, self.manager).status_code, 200)
        claim.refresh_from_db()
        self.assertIsNone(claim.accounting_approved_at)

    def test_web_approval_invalidates_old_manager_prompt_without_new_cfo_approval(self):
        self.share_chat()
        claim, _ = self.make_claim('excess-manager')
        old = self.scoped(claim, self.manager)
        self.manager.signature.name = 'signatures/test/manager.png'
        self.manager.save()
        self.client.force_login(self.manager)
        self.client.post(reverse('claim_approval_action', args=[claim.pk]), {'action': 'approve'})
        self.click(old, self.manager)
        claim.refresh_from_db()
        self.assertIsNotNone(claim.excess_accounting_approved_at)
        self.assertIsNone(claim.excess_cfo_approved_at)
        self.assertEqual(claim.current_approver(), self.cfo)


@override_settings(TELEGRAM_BOT_TOKEN='', TELEGRAM_WEBHOOK_SECRET='parallel-secret')
class TelegramConcurrentCallbackTests(TransactionTestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        # TransactionTestCase ล้างกลุ่มจาก data migration ด้วย คืนข้อมูลตั้งต้น
        # ให้ชุดทดสอบสิทธิ์เดิมเมื่อใช้ --keepdb ต่อ โดยไม่แตะฐานข้อมูลจริง
        for name in ROLE_NAMES:
            Group.objects.get_or_create(name=name)

    def _check_simultaneous(self, duplicate_id):
        requester = User.objects.create_user(username='parallel-requester')
        manager = User.objects.create_user(username='parallel-manager', telegram_user_id='997711')
        cfo = User.objects.create_user(username='parallel-cfo', telegram_user_id='997711')
        accountant = User.objects.create_user(username='parallel-accountant')
        claim = ClaimRequest.objects.create(requester=requester, accounting_manager=manager,
            cfo=cfo, accountant=accountant, request_type='ADVANCE',
            status=ClaimRequest.Status.PENDING_EXCESS_APPROVAL, requested_total=Decimal('4000'),
            actual_total=Decimal('5500'), excess_cfo_required=True, purpose='Concurrent retry', work_location='HQ')
        data = telegram_approval_callback_data(claim, 'approve', actor=manager)
        payload = json.dumps({'callback_query': {'id': 'same-parallel-callback', 'data': data,
            'from': {'id': 997711}}})
        barrier = Barrier(2)

        def deliver(index):
            try:
                barrier.wait(timeout=15)
                delivery = json.loads(payload)
                if not duplicate_id:
                    delivery['callback_query']['id'] += f'-{index}'
                return Client().post(reverse('telegram_webhook'), data=json.dumps(delivery),
                    content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='parallel-secret').status_code
            finally:
                connections.close_all()

        with patch('apps.claims.views.send_telegram_notification'), patch('apps.claims.views.send_telegram_group_notification'), \
                patch('apps.claims.views.answer_telegram_callback'), patch('apps.claims.views.remove_telegram_callback_keyboard'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(deliver, range(2)))
        self.assertEqual(results, [200, 200])
        claim.refresh_from_db()
        self.assertIsNotNone(claim.excess_accounting_approved_at)
        self.assertIsNone(claim.excess_cfo_approved_at)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_EXCESS_APPROVAL)
        self.assertEqual(claim.approval_records.filter(action='APPROVED').count(), 1)
        self.assertEqual(TelegramCallbackReceipt.objects.filter(callback_id__startswith='same-parallel-callback').count(), 1 if duplicate_id else 2)
        self.assertEqual(AdminAuditLog.objects.filter(action='APPROVE_CLAIM').count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=cfo).count(), 1)
        self.assertFalse(Notification.objects.filter(recipient=accountant).exists())

    def test_simultaneous_same_callback_with_shared_chat_advances_only_once(self):
        self._check_simultaneous(duplicate_id=True)

    def test_simultaneous_different_clicks_on_old_manager_button_cannot_approve_cfo(self):
        self._check_simultaneous(duplicate_id=False)
