from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.roles import ACCOUNTANT, REQUESTER


class ApprovalConfigurationCheckTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='approval-check-admin')
        self.admin.signature.name = 'signatures/test/admin.png'
        self.admin.save()
        self.supervisor = User.objects.create_user(username='approval-check-supervisor')
        self.manager = User.objects.create_user(username='approval-check-manager')
        self.payer = User.objects.create_user(username='approval-check-payer')
        self.payer.groups.add(Group.objects.get(name=ACCOUNTANT))
        self.requester = User.objects.create_user(username='approval-check-requester',
            first_name='Test', last_name='Requester', department='IT',
            supervisor=self.supervisor, accounting_manager=self.manager, accountant=self.payer)
        self.requester.groups.add(Group.objects.get(name=REQUESTER))
        self.client.force_login(self.admin)

    def get(self):
        response = self.client.get(reverse('admin_operations'))
        self.assertEqual(response.status_code, 200)
        return response

    def test_complete_current_chain_does_not_require_legacy_executive(self):
        self.assertIsNone(self.requester.executive_id)
        response = self.get()
        self.assertEqual(response.context['incomplete_users'], [])
        self.assertContains(response, 'สายอนุมัติผู้ใช้ครบแล้ว')
        self.assertNotContains(response, 'ขาด ผู้บริหาร')
        self.requester.refresh_from_db()
        self.assertIsNone(self.requester.executive_id)

    def test_missing_fields_are_badges_with_user_department_and_edit_button(self):
        self.requester.supervisor = None
        self.requester.accountant = None
        self.requester.save(update_fields=['supervisor', 'accountant'])
        response = self.get()
        item = response.context['incomplete_users'][0]
        self.assertEqual(item['missing_fields'], ['ผู้อนุมัติ', 'เจ้าหน้าที่บัญชี / ผู้จ่ายสำรอง'])
        self.assertContains(response, 'ต้องแก้ไข 1 คน')
        self.assertContains(response, 'Test Requester')
        self.assertContains(response, 'แผนก IT')
        self.assertContains(response, 'approval-check-badge', count=2)
        self.assertContains(response, reverse('user_edit', args=[self.requester.pk]))

    def test_accountant_requester_with_valid_alternate_payer_is_complete(self):
        self.requester.groups.add(Group.objects.get(name=ACCOUNTANT))
        self.requester.accountant = None
        self.requester.alternate_payer = self.payer
        self.requester.save(update_fields=['accountant', 'alternate_payer'])
        self.assertEqual(self.get().context['incomplete_users'], [])

    def test_inactive_supervisor_manager_and_payer_are_reported(self):
        User.objects.filter(pk__in=[self.supervisor.pk, self.manager.pk, self.payer.pk]).update(is_active=False)
        fields = self.get().context['incomplete_users'][0]['missing_fields']
        self.assertEqual(fields, ['ผู้อนุมัติ (บัญชีปิดใช้งาน)', 'ผู้จัดการบัญชี (บัญชีปิดใช้งาน)', 'เจ้าหน้าที่บัญชี / ผู้จ่ายสำรอง'])

    def test_invalid_alternate_payer_and_self_payment_are_not_considered_complete(self):
        self.requester.groups.add(Group.objects.get(name=ACCOUNTANT))
        self.requester.accountant = self.requester
        self.requester.save(update_fields=['accountant'])
        self.assertEqual(self.get().context['incomplete_users'][0]['missing_fields'], ['เจ้าหน้าที่บัญชี / ผู้จ่ายสำรอง'])
        self.requester.alternate_payer = self.supervisor
        self.requester.save(update_fields=['alternate_payer'])
        self.assertEqual(len(self.get().context['incomplete_users']), 1)

    def test_inactive_requesters_and_nonrequester_users_are_excluded(self):
        self.requester.is_active = False
        self.requester.supervisor = None
        self.requester.save(update_fields=['is_active', 'supervisor'])
        self.assertEqual(self.get().context['incomplete_users'], [])
