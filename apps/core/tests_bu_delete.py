from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.roles import ACCOUNTANT, ACCOUNTING_APPROVER, SUPER_ADMIN
from apps.claims.models import ClaimRequest
from .models import AdminAuditLog, BusinessUnit
from .system_settings import get_system_settings


class BusinessUnitDeleteTests(TestCase):
    def setUp(self):
        self.accountant = User.objects.create_user(username='delete-bu-accountant')
        self.accountant.groups.add(Group.objects.get(name=ACCOUNTANT))
        self.accountant.signature.name = 'signatures/test/bu-delete.png'
        self.accountant.save()
        self.client.force_login(self.accountant)
        self.unit = BusinessUnit.objects.create(code='TEST_DELETE', name='Test unit')
        self.url = reverse('business_unit_delete', args=[self.unit.pk])

    def test_delete_is_post_only(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertTrue(BusinessUnit.objects.filter(pk=self.unit.pk).exists())
        self.assertFalse(AdminAuditLog.objects.filter(action='DELETE_BU').exists())

    def test_authorized_roles_can_delete_with_audit(self):
        manager = User.objects.create_user(username='delete-bu-manager')
        manager.groups.add(Group.objects.get(name=ACCOUNTING_APPROVER))
        admin = User.objects.create_user(username='delete-bu-admin')
        admin.groups.add(Group.objects.get(name=SUPER_ADMIN))
        superuser = User.objects.create_superuser(username='delete-bu-superuser')
        cfo = User.objects.create_user(username='delete-bu-cfo')
        settings = get_system_settings()
        settings.cfo = cfo
        settings.save(update_fields=['cfo'])
        for index, user in enumerate((self.accountant, manager, admin, superuser, cfo)):
            user.signature.name = 'signatures/test/bu-delete.png'
            user.save(update_fields=['signature'])
            self.client.force_login(user)
            unit = BusinessUnit.objects.create(code=f'DEL_{index}', name=f'Unit {index}')
            response = self.client.post(reverse('business_unit_delete', args=[unit.pk]))
            self.assertRedirects(response, reverse('budget_settings'))
            self.assertFalse(BusinessUnit.objects.filter(pk=unit.pk).exists())
            audit = AdminAuditLog.objects.get(action='DELETE_BU', target=unit.code)
            self.assertEqual(audit.actor, user)
            self.assertEqual(audit.detail, unit.name)

    def test_regular_user_cannot_delete_even_with_direct_post(self):
        user = User.objects.create_user(username='delete-bu-regular')
        user.signature.name = 'signatures/test/bu-delete.png'
        user.save()
        self.client.force_login(user)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertTrue(BusinessUnit.objects.filter(pk=self.unit.pk).exists())
        self.assertFalse(AdminAuditLog.objects.filter(action='DELETE_BU').exists())

    def test_delete_preserves_existing_claim_item_and_user_bu(self):
        self.accountant.business_unit = self.unit.code
        self.accountant.save(update_fields=['business_unit'])
        claim = ClaimRequest.objects.create(requester=self.accountant, purpose='Historical claim',
            work_location='HQ', business_unit=self.unit.code)
        item = claim.items.create(description='Historical item', requested_amount='100.00', business_unit=self.unit.code)
        self.client.post(self.url)
        claim.refresh_from_db()
        item.refresh_from_db()
        self.accountant.refresh_from_db()
        self.assertEqual(claim.business_unit, self.unit.code)
        self.assertEqual(item.business_unit, self.unit.code)
        self.assertEqual(self.accountant.business_unit, self.unit.code)
        self.assertFalse(BusinessUnit.objects.filter(pk=self.unit.pk).exists())
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertEqual(AdminAuditLog.objects.filter(action='DELETE_BU', target=self.unit.code).count(), 1)

    def test_delete_requires_login_and_csrf(self):
        self.client.logout()
        self.assertEqual(self.client.post(self.url).status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.accountant)
        self.assertEqual(csrf_client.post(self.url).status_code, 403)
        self.assertTrue(BusinessUnit.objects.filter(pk=self.unit.pk).exists())

    def test_finance_delete_button_has_confirmation_and_csrf(self):
        response = self.client.get(reverse('budget_settings'))
        self.assertContains(response, self.url)
        self.assertContains(response, 'ยืนยันลบ BU')
        self.assertContains(response, 'csrfmiddlewaretoken')
