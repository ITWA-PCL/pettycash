from datetime import date
from io import BytesIO
import json
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from apps.accounts.forms import UserManagementForm
from apps.accounts.models import User
from apps.accounts.roles import ACCOUNTANT, SUPERVISOR_APPROVER
from apps.core.system_settings import get_system_settings
from .forms import ClaimRequestForm
from .models import ClaimRequest


@override_settings(TELEGRAM_BOT_TOKEN='')
class ExpenseMonthPayerTests(TestCase):
    def setUp(self):
        # ตรึงวันที่เฉพาะชุดเดิมที่ทดสอบเรื่องอื่น ไม่ให้วันที่ตัวอย่างเก่าหมดอายุ
        self.policy_clock = patch('apps.claims.policies.current_date', return_value=date(2026, 9, 19))
        self.policy_clock.start()
        self.addCleanup(self.policy_clock.stop)
        self.owner = User.objects.create_user(username='payer-owner')
        self.other = User.objects.create_user(username='payer-other')
        self.manager = User.objects.create_user(username='payer-manager')
        self.cfo = User.objects.create_user(username='payer-cfo')
        role, _ = Group.objects.get_or_create(name=ACCOUNTANT)
        self.owner.groups.add(role)
        self.other.groups.add(role)
        approver, _ = Group.objects.get_or_create(name=SUPERVISOR_APPROVER)
        self.manager.groups.add(approver)
        self.owner.supervisor = self.manager
        self.owner.accounting_manager = self.manager
        self.owner.alternate_payer = self.other
        self.owner.signature.name = 'signatures/test.png'
        self.owner.save()
        self.other.signature.name = 'signatures/other.png'
        self.other.save()
        config = get_system_settings()
        config.cfo = self.cfo
        config.save()

    def make_claim(self, **kwargs):
        fields = dict(requester=self.owner, accountant=self.other, supervisor=self.manager,
                      accounting_manager=self.manager, cfo=self.cfo, request_type='ADVANCE',
                      request_date=date(2026, 10, 20), expense_month=date(2026, 10, 1), purpose='Expense', work_location='HQ', requested_total='100.00')
        fields.update(kwargs)
        claim = ClaimRequest.objects.create(**fields)
        claim.items.create(description='Item', requested_amount='100.00', department=self.owner.department)
        claim.refresh_from_db()
        return claim

    def test_month_is_independent_and_survives_edit(self):
        data = dict(request_type='ADVANCE', request_date='20/10/2026', expense_month='2026-11',
                    supervisor=self.manager.pk, work_location='HQ', purpose='Expense')
        form = ClaimRequestForm(data, requester=self.owner)
        self.assertTrue(form.is_valid(), form.errors)
        claim = form.save()
        self.assertEqual(claim.request_date, date(2026, 10, 20))
        self.assertEqual(claim.expense_month, date(2026, 11, 1))
        self.assertEqual(claim.accountant, self.other)
        edit = ClaimRequestForm(instance=claim, requester=self.owner)
        self.assertIn('value="2026-11"', str(edit['expense_month']))
        self.assertIn('พฤศจิกายน 2569', str(edit['expense_month']))
        self.assertIn('data-thai-month-picker', str(edit['expense_month']))
        data['expense_month'] = '2026-13'
        self.assertFalse(ClaimRequestForm(data, requester=self.owner).is_valid())

    def test_legacy_draft_without_month_must_choose_before_submit(self):
        claim = self.make_claim(expense_month=None)
        self.assertIsNone(claim.expense_month)
        self.assertFalse(claim.can_submit())
        self.client.force_login(self.owner)
        self.client.post(reverse('claim_submit', args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.DRAFT)

    def test_month_is_required_on_create_and_edit(self):
        data = dict(request_type='ADVANCE', request_date='20/10/2026',
                    supervisor=self.manager.pk, work_location='HQ', purpose='Expense')
        for instance in (None, self.make_claim()):
            form = ClaimRequestForm(data, requester=self.owner, instance=instance)
            self.assertFalse(form.is_valid())
            self.assertIn('expense_month', form.errors)
            self.assertTrue(form.fields['expense_month'].required)

    @patch('apps.claims.views.notify_next_approvers')
    def test_submit_snapshots_alternate_and_routes_to_own_approver(self, notify):
        claim = self.make_claim(accountant=None)
        self.client.force_login(self.owner)
        self.client.post(reverse('claim_submit', args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.accountant, self.other)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)
        notify.assert_called_once()

    def test_fallback_to_other_assigned_accountant(self):
        self.owner.alternate_payer = None
        self.owner.accountant = self.other
        self.assertEqual(self.owner.claim_payer(), self.other)
        self.owner.accountant = self.owner
        self.assertIsNone(self.owner.claim_payer())

    def test_inactive_alternate_blocks_submission(self):
        self.other.is_active = False
        self.other.save()
        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.claim_payer())
        claim = self.make_claim()
        self.assertFalse(claim.can_submit())
        self.client.force_login(self.owner)
        self.client.post(reverse('claim_submit', args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.DRAFT)

    def test_user_dropdown_excludes_self_and_non_accountants(self):
        form = UserManagementForm(instance=self.owner)
        choices = form.fields['alternate_payer'].queryset
        self.assertIn(self.other, choices)
        self.assertNotIn(self.owner, choices)
        self.assertNotIn(self.manager, choices)

    def test_even_superuser_cannot_pay_own_initial_or_supplemental_claim(self):
        self.owner.is_superuser = True
        self.owner.save()
        self.client.force_login(self.owner)
        for actual in (None, '125.00'):
            claim = self.make_claim(status=ClaimRequest.Status.PENDING_PAYMENT, accountant=self.owner, actual_total=actual)
            self.assertFalse(claim.can_submit())
            response = self.client.get(reverse('claim_detail', args=[claim.pk]))
            self.assertFalse(response.context['can_pay'])
            self.client.post(reverse('claim_payment', args=[claim.pk]), {'payment_method': 'CASH'})
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
            self.assertFalse(claim.payments.exists())

    def test_other_accountant_can_pay_initial_and_supplemental(self):
        self.client.force_login(self.other)
        for actual, amount in ((None, '100.00'), ('125.00', '25.00')):
            claim = self.make_claim(status=ClaimRequest.Status.PENDING_PAYMENT, actual_total=actual)
            self.client.post(reverse('claim_payment', args=[claim.pk]), {'payment_method': 'CASH'})
            claim.refresh_from_db()
            self.assertEqual(claim.status, ClaimRequest.Status.PAID)
            self.assertEqual(str(claim.payments.get().amount), amount)

    def test_month_visible_in_detail(self):
        claim = self.make_claim(expense_month=date(2026, 11, 1))
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('claim_detail', args=[claim.pk])), 'พฤศจิกายน 2569')

    @patch('apps.claims.views.notify_next_approvers')
    def test_regular_claim_keeps_existing_assigned_payer(self, notify):
        self.owner.groups.clear()
        self.owner.alternate_payer = None
        self.owner.save()
        claim = self.make_claim()
        self.client.force_login(self.owner)
        self.client.post(reverse('claim_submit', args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.accountant, self.other)
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_SUPERVISOR)

    @override_settings(TELEGRAM_WEBHOOK_SECRET='payer-test-secret')
    @patch('apps.claims.views.answer_telegram_callback')
    @patch('apps.claims.views.remove_telegram_callback_keyboard')
    def test_legacy_telegram_pay_callback_cannot_bypass_web_payment(self, remove, answer):
        claim = self.make_claim(status=ClaimRequest.Status.PENDING_PAYMENT, accountant=self.owner)
        payload = {'callback_query': {'id': 'self-pay', 'data': f'pc:{claim.pk}:pay', 'from': {'id': 100}}}
        response = self.client.post(reverse('telegram_webhook'), data=json.dumps(payload),
            content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='payer-test-secret')
        self.assertEqual(response.status_code, 200)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ClaimRequest.Status.PENDING_PAYMENT)
        self.assertFalse(claim.payments.exists())

    def test_month_in_export(self):
        self.make_claim(expense_month=date(2026, 11, 1))
        self.owner.is_superuser = True
        self.owner.save()
        self.client.force_login(self.owner)
        response = self.client.get(reverse('claim_export'))
        self.assertEqual(response.status_code, 200)
        sheet = load_workbook(BytesIO(response.content)).active
        self.assertEqual(sheet.cell(5, 3).value, 'ค่าใช้จ่ายของเดือน')
        self.assertEqual(sheet.cell(6, 3).value, 'พฤศจิกายน 2569')

    def test_thai_month_input_converts_buddhist_year_without_changing_request_date(self):
        data = dict(request_type='ADVANCE', request_date='20/10/2026',
                    expense_month_0='11', expense_month_1='2569',
                    supervisor=self.manager.pk, work_location='HQ', purpose='Expense')
        form = ClaimRequestForm(data, requester=self.owner)
        self.assertTrue(form.is_valid(), form.errors)
        claim = form.save()
        self.assertEqual(claim.expense_month, date(2026, 11, 1))
        self.assertEqual(claim.request_date, date(2026, 10, 20))
        self.assertEqual(claim.expense_month_display, 'พฤศจิกายน 2569')
        for month, year in [('11', ''), ('', '2569'), ('13', '2569'), ('11', '0')]:
            data.update(expense_month_0=month, expense_month_1=year)
            self.assertFalse(ClaimRequestForm(data, requester=self.owner).is_valid())
