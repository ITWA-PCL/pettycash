from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.db import connections
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.roles import ACCOUNTANT, SUPERVISOR_APPROVER, ROLE_NAMES
from apps.core.system_settings import get_system_settings
from .forms import ClaimRequestForm
from .models import ClaimApproval, ClaimDocumentSequence, ClaimPayment, ClaimRequest
from .policies import backdate_error
from .telegram_totals import daily_approval_breakdown
from .views import assign_document_number


@override_settings(TELEGRAM_BOT_TOKEN='')
class DemoRequirementsTests(TestCase):
    def setUp(self):
        self.clock = patch('apps.claims.policies.current_date', return_value=date(2026, 11, 5)).start()
        self.number_clock = patch('apps.claims.views.timezone.localdate', return_value=date(2026, 11, 5)).start()
        self.addCleanup(patch.stopall)
        for name in ('requester', 'supervisor', 'manager', 'accountant', 'cfo', 'other'):
            user = User.objects.create_user(username=f'demo-new-{name}', department='IT')
            user.signature.name = 'signatures/test/demo.png'
            user.save()
            setattr(self, name, user)
        self.supervisor.groups.add(Group.objects.get(name=SUPERVISOR_APPROVER))
        self.accountant.groups.add(Group.objects.get(name=ACCOUNTANT))
        self.requester.supervisor = self.supervisor
        self.requester.accounting_manager = self.manager
        self.requester.accountant = self.accountant
        self.requester.save()
        self.config = get_system_settings()
        self.config.cfo = self.cfo
        self.config.save()
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        context = self.settings(MEDIA_ROOT=media.name)
        context.enable()
        self.addCleanup(context.disable)

    def claim(self, amount='100', **kwargs):
        fields = dict(requester=self.requester, supervisor=self.supervisor,
            accounting_manager=self.manager, accountant=self.accountant, cfo=self.cfo,
            request_date=date(2026, 11, 5), expense_month=date(2026, 12, 1),
            work_location='HQ', purpose='Demo requirements', requested_total=Decimal(amount),
            approval_threshold=Decimal('5000'))
        fields.update(kwargs)
        claim = ClaimRequest.objects.create(**fields)
        claim.items.create(description='Expense', department='IT', budget='Office', requested_amount=Decimal(amount))
        return claim

    def form(self, request_date, instance=None, **kwargs):
        data = dict(request_type='ADVANCE', request_date=request_date, expense_month='2027-01',
            work_location='HQ', supervisor=self.supervisor.pk, purpose='Independent expense month')
        data.update(kwargs)
        return ClaimRequestForm(data, instance=instance, requester=self.requester)

    def submit(self, claim):
        self.client.force_login(claim.requester)
        response = self.client.post(reverse('claim_submit', args=[claim.pk]))
        self.assertEqual(response.status_code, 302)
        claim.refresh_from_db()
        return response

    def pay_record(self, claim, amount, kind=ClaimPayment.Kind.INITIAL):
        return claim.payments.create(kind=kind, method='CASH', amount=Decimal(amount), paid_by=self.accountant)

    def test_five_days_inclusive_and_future_expense_month_independent(self):
        form = self.form('31/10/2026')
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.expense_month, date(2027, 1, 1))
        self.assertEqual(saved.request_date, date(2026, 10, 31))
        old = self.form('30/10/2026')
        self.assertFalse(old.is_valid())
        self.assertIn('request_date', old.errors)

    def test_saved_old_draft_cannot_bypass_five_day_submission_check(self):
        claim = self.claim(request_date=date(2026, 10, 30))
        self.submit(claim)
        self.assertEqual(claim.status, 'DRAFT')
        self.assertIsNone(claim.document_number)
        self.assertFalse(claim.approval_records.exists())

    def test_returned_after_deadline_can_resubmit_only_same_previously_valid_date(self):
        claim = self.claim(request_date=date(2026, 10, 31))
        self.submit(claim)
        self.assertEqual(claim.accepted_request_date, date(2026, 10, 31))
        old_number = claim.document_number
        self.client.force_login(self.supervisor)
        self.client.post(reverse('claim_approval_action', args=[claim.pk]), {'action': 'approve'})
        self.client.force_login(self.manager)
        self.client.post(reverse('claim_approval_action', args=[claim.pk]), {'action': 'return', 'comment': 'แก้รายละเอียด'})
        claim.refresh_from_db()
        self.clock.return_value = date(2026, 11, 20)
        self.assertTrue(self.form('31/10/2026', instance=claim).is_valid())
        self.assertFalse(self.form('30/10/2026', instance=claim).is_valid())
        claim.refresh_from_db()
        self.submit(claim)
        self.assertEqual(claim.status, 'PENDING_SUPERVISOR')
        self.assertEqual(claim.document_number, old_number)

    def test_new_location_other_rejected_but_legacy_detail_kept(self):
        form = self.form('05/11/2026', work_location='OTHER', branch_name='Legacy place')
        self.assertFalse(form.is_valid())
        self.assertIn('work_location', form.errors)
        self.assertNotIn('branch_name', form.fields)
        old = self.claim(work_location='OTHER', branch_name='Legacy place', status='COMPLETED')
        self.client.force_login(self.requester)
        self.assertContains(self.client.get(reverse('claim_detail', args=[old.pk])), 'Legacy place')
        draft = self.claim(work_location='OTHER')
        self.submit(draft)
        self.assertEqual(draft.status, 'DRAFT')

    def test_cross_day_unpaid_3000_plus_2500_requires_ordered_manager_and_cfo(self):
        previous = self.claim('3000', request_date=date(2026, 11, 4), status='PENDING_PAYMENT', document_number='BR-69110001')
        current = self.claim('2500')
        self.submit(current)
        self.assertEqual(current.daily_requested_total, Decimal('5500'))
        self.assertTrue(current.requires_cfo_approval)
        self.assertEqual(current.approval_exposure_details[0]['id'], previous.pk)
        self.assertIn('ค้างจ่ายข้ามวัน', current.approval_exposure_details[0]['basis'])
        self.client.force_login(self.supervisor)
        self.client.post(reverse('claim_approval_action', args=[current.pk]), {'action': 'approve'})
        current.refresh_from_db()
        self.assertEqual(current.current_approver(), self.manager)
        self.assertFalse(current.can_approve(self.cfo))
        self.client.force_login(self.manager)
        self.client.post(reverse('claim_approval_action', args=[current.pk]), {'action': 'approve'})
        current.refresh_from_db()
        self.assertEqual(current.current_approver(), self.cfo)

    def test_cross_day_only_outstanding_supplemental_not_original_paid_amount(self):
        previous = self.claim('4000', actual_total=Decimal('5500'), request_date=date(2026, 11, 3), status='PENDING_EXCESS_APPROVAL')
        self.pay_record(previous, '4000')
        current = self.claim('3501')
        current.capture_exposure()
        self.assertEqual(current.daily_requested_total, Decimal('5001'))
        self.assertEqual(current.approval_exposure_details[0]['amount'], '1500.00')
        self.assertIn('ส่วนต่าง', current.approval_exposure_details[0]['basis'])
        self.pay_record(previous, '1500', ClaimPayment.Kind.SUPPLEMENTAL)
        self.assertEqual(current.calculate_daily_total(), Decimal('3501'))

    def test_legacy_paid_initial_without_ledger_does_not_count_full_loan_again(self):
        previous = self.claim('4000', actual_total=Decimal('5500'), request_date=date(2026, 11, 3), status='PENDING_EXCESS_APPROVAL')
        previous.approval_records.create(actor=self.accountant, action='PAID', status_before='PENDING_PAYMENT', status_after='PAID')
        current = self.claim('3500')
        self.assertEqual(current.calculate_daily_total(), Decimal('5000'))

    def test_same_day_actual_and_cross_day_unpaid_boundary_exactly_5000(self):
        self.claim('100', actual_total=Decimal('80'), status='COMPLETED')
        self.claim('3000', request_date=date(2026, 11, 4), status='PENDING_PAYMENT')
        self.claim('9999', request_date=date(2026, 11, 4), status='COMPLETED')
        self.claim('9999', request_date=date(2026, 11, 4), status='RETURNED')
        other = self.claim('9999', request_date=date(2026, 11, 4), status='PENDING_PAYMENT', requester=self.other)
        current = self.claim('1920')
        current.capture_exposure()
        self.assertEqual(current.daily_requested_total, Decimal('5000'))
        self.assertFalse(current.requires_cfo_approval)
        self.assertNotIn(other.pk, [row['id'] for row in current.approval_exposure_details])
        current.requested_total += Decimal('.01')
        current.capture_exposure()
        self.assertTrue(current.requires_cfo_approval)

    def test_same_day_paid_claim_stays_in_daily_total_cross_day_paid_claim_does_not(self):
        previous = self.claim('4000', status='PAID')
        self.pay_record(previous, '4000')
        current = self.claim('1500')
        self.assertEqual(current.calculate_daily_total(), Decimal('5500'))
        previous.request_date = date(2026, 11, 4)
        previous.save(update_fields=['request_date'])
        self.assertEqual(current.calculate_daily_total(), Decimal('1500'))

    def test_snapshot_details_stay_frozen_after_previous_payment(self):
        previous = self.claim('3000', request_date=date(2026, 11, 4), status='PENDING_PAYMENT', document_number='BR-69110001')
        current = self.claim('2500')
        self.submit(current)
        self.pay_record(previous, '3000')
        previous.status = 'PAID'
        previous.save(update_fields=['status'])
        current.refresh_from_db()
        self.assertEqual(current.daily_requested_total, Decimal('5500'))
        text = daily_approval_breakdown(current, current.daily_requested_total)
        self.assertIn('BR-69110001: 3,000.00 บาท', text)
        self.assertIn('บันทึก ณ ยื่น', text)
        self.assertEqual(current.calculate_daily_total(), Decimal('2500'))

    def test_cfo_compares_source_and_new_documents_with_scoped_private_preview(self):
        source = self.claim('3000', request_date=date(2026, 11, 4), status='PENDING_PAYMENT', cfo=self.other,
            document_number='BR-69110001', purpose='Prior document for CFO')
        attachment = source.attachments.create(file=SimpleUploadedFile('prior.pdf', b'%PDF-1.4 test'),
            original_name='prior.pdf', uploaded_by=self.requester)
        current = self.claim('2500')
        self.submit(current)
        self.client.force_login(self.cfo)
        detail = self.client.get(reverse('claim_detail', args=[current.pk]))
        self.assertContains(detail, 'Prior document for CFO')
        self.assertContains(detail, 'prior.pdf')
        self.assertContains(detail, 'ยอดที่นำมารวมตอนยื่น')
        url = reverse('claim_reference_preview', args=[current.pk, source.pk, attachment.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(b''.join(response.streaming_content), b'%PDF-1.4 test')
        self.client.force_login(self.supervisor)
        self.assertNotContains(self.client.get(reverse('claim_detail', args=[current.pk])), 'Prior document for CFO')
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_reference_cannot_read_unlisted_or_other_requester_attachment(self):
        source = self.claim('3000', request_date=date(2026, 11, 4), status='PENDING_PAYMENT')
        current = self.claim('2500')
        self.submit(current)
        unrelated = self.claim('100', requester=self.other)
        file = unrelated.attachments.create(file=SimpleUploadedFile('other.pdf', b'%PDF'), original_name='other.pdf', uploaded_by=self.other)
        self.client.force_login(self.cfo)
        for source_id in (source.pk, unrelated.pk):
            self.assertEqual(self.client.get(reverse('claim_reference_preview', args=[current.pk, source_id, file.pk])).status_code, 404)

    def test_deleted_source_keeps_snapshot_number_amount_but_no_file_access(self):
        source = self.claim('3000', request_date=date(2026, 11, 4), status='PENDING_PAYMENT', document_number='BR-69110001')
        current = self.claim('2500')
        self.submit(current)
        source.delete()
        self.client.force_login(self.cfo)
        response = self.client.get(reverse('claim_detail', args=[current.pk]))
        self.assertContains(response, 'BR-69110001')
        self.assertContains(response, 'ไม่พบใบต้นฉบับแล้ว')
        current.refresh_from_db()
        self.assertEqual(current.daily_requested_total, Decimal('5500'))

    def test_monthly_numbers_separate_hq_branch_reset_month_and_preserve_old(self):
        first, second, branch = self.claim(), self.claim(), self.claim(work_location='BRANCH')
        for claim in (first, second, branch):
            assign_document_number(claim)
            claim.save(update_fields=['document_number'])
        self.assertEqual(first.document_number, 'HO-69110001')
        self.assertEqual(second.document_number, 'HO-69110002')
        self.assertEqual(branch.document_number, 'BR-69110001')
        with patch('apps.claims.views.timezone.localdate', return_value=date(2026, 12, 1)):
            december = self.claim()
            assign_document_number(december)
            self.assertEqual(december.document_number, 'HO-69120001')
            assign_document_number(first)
            self.assertEqual(first.document_number, 'HO-69110001')
        old = self.claim(document_number='BR-69102901')
        assign_document_number(old)
        self.assertEqual(old.document_number, 'BR-69102901')

    def test_configured_location_prefixes_apply_only_to_new_numbers_and_can_restore_counter(self):
        old = self.claim()
        assign_document_number(old)
        old.save(update_fields=['document_number'])
        self.config.head_office_document_prefix = 'HEADQUARTERS'
        self.config.branch_document_prefix = 'KAN'
        self.config.save()
        office, branch = self.claim(), self.claim(work_location='BRANCH')
        for claim in (office, branch):
            assign_document_number(claim)
            claim.save(update_fields=['document_number'])
            claim.refresh_from_db()
        self.assertEqual(office.document_number, 'HEADQUARTERS-69110001')
        self.assertEqual(branch.document_number, 'KAN-69110001')
        assign_document_number(old)
        self.assertEqual(old.document_number, 'HO-69110001')
        self.config.head_office_document_prefix = 'HO'
        self.config.save()
        restored = self.claim()
        assign_document_number(restored)
        self.assertEqual(restored.document_number, 'HO-69110002')

    def test_number_counter_skips_existing_numbers_and_does_not_reuse_deleted_document(self):
        old = self.claim(document_number='HO-69110001')
        current = self.claim()
        assign_document_number(current)
        self.assertEqual(current.document_number, 'HO-69110002')
        current.save(update_fields=['document_number'])
        current.delete()
        following = self.claim()
        assign_document_number(following)
        self.assertEqual(following.document_number, 'HO-69110003')
        old.refresh_from_db()
        self.assertEqual(old.document_number, 'HO-69110001')

    def test_year_reset_and_sequence_exhaustion_do_not_touch_legacy_documents(self):
        with patch('apps.claims.views.timezone.localdate', return_value=date(2027, 1, 1)):
            claim = self.claim()
            assign_document_number(claim)
            self.assertEqual(claim.document_number, 'HO-70010001')
        ClaimDocumentSequence.objects.create(prefix='HO', period='202611', last_number=9999)
        claim = self.claim()
        with self.assertRaises(ValueError):
            assign_document_number(claim)
        self.assertIsNone(claim.document_number)

    def test_petty_three_days_exact_boundary_and_advance_one_day_unchanged(self):
        self.assertEqual(self.config.petty_receive_after_days, 3)
        self.assertEqual(self.config.auto_receive_after_days, 1)
        now = datetime(2026, 11, 5, 10, tzinfo=dt_timezone.utc)
        waiting = self.claim(request_type='PETTY_CASH', status='PAID', actual_total=Decimal('100'))
        due = self.claim(request_type='PETTY_CASH', status='PAID', actual_total=Decimal('100'))
        for claim, elapsed in ((waiting, timedelta(days=3) - timedelta(seconds=1)), (due, timedelta(days=3))):
            event = claim.approval_records.create(actor=self.accountant, action='PAID', status_before='PENDING_PAYMENT', status_after='PAID')
            ClaimApproval.objects.filter(pk=event.pk).update(created_at=now-elapsed)
        with patch('apps.core.management.commands.auto_confirm_receipts.timezone.now', return_value=now), \
                patch('apps.core.management.commands.auto_confirm_receipts.notify'):
            call_command('auto_confirm_receipts', stdout=StringIO())
        waiting.refresh_from_db()
        due.refresh_from_db()
        self.assertEqual(waiting.status, 'PAID')
        self.assertEqual(due.status, 'COMPLETED')


class MonthlyNumberConcurrencyTests(TransactionTestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for name in ROLE_NAMES:
            Group.objects.get_or_create(name=name)

    def test_two_people_submitting_first_document_of_month_get_distinct_numbers(self):
        users = [User.objects.create_user(username=f'number-person-{index}') for index in range(2)]
        barrier = Barrier(2)

        def create_document(user_id):
            try:
                claim = ClaimRequest.objects.create(requester_id=user_id, work_location='HQ', purpose='Parallel numbering')
                barrier.wait(timeout=15)
                assign_document_number(claim)
                claim.save(update_fields=['document_number'])
                return claim.document_number
            finally:
                connections.close_all()

        with patch('apps.claims.views.timezone.localdate', return_value=date(2026, 11, 1)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                numbers = list(pool.map(create_document, [user.pk for user in users]))
        self.assertEqual(set(numbers), {'HO-69110001', 'HO-69110002'})
        self.assertEqual(ClaimDocumentSequence.objects.get(prefix='HO', period='202611').last_number, 2)
