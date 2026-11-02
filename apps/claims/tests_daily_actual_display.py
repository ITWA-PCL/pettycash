from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from .models import ClaimApproval, ClaimRequest


class DailyActualDisplayTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username='actual-day-requester')
        self.supervisor = User.objects.create_user(username='actual-day-supervisor')
        self.supervisor.signature.name = 'signatures/test/sign.png'
        self.supervisor.save()
        self.requester.signature.name = 'signatures/test/sign.png'
        self.requester.save()
        self.claim = self.make_claim('4950', '4950', daily_requested_total='5050', supervisor=self.supervisor)
        self.previous = self.make_claim('100', '80')
        self.client.force_login(self.requester)

    def make_claim(self, requested, actual, **kwargs):
        fields = dict(requester=self.requester, request_date=date(2026, 10, 24),
            status=ClaimRequest.Status.COMPLETED, requested_total=requested, actual_total=actual,
            purpose='Daily actual display', work_location='HQ')
        fields.update(kwargs)
        return ClaimRequest.objects.create(**fields)

    def detail(self):
        return self.client.get(reverse('claim_detail', args=[self.claim.pk]))

    def test_display_5030_alongside_unchanged_historical_snapshot(self):
        response = self.detail()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ยอดใช้จริงสะสมของวันที่ขอเบิก (24/10/2026)')
        self.assertContains(response, '5030.00 บาท')
        self.assertContains(response, '5050.00 บาท')
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.daily_requested_total, Decimal('5050'))
        previous_response = self.client.get(reverse('claim_detail', args=[self.previous.pk]))
        self.assertEqual(previous_response.context['claim'].completed_daily_actual_total, Decimal('5030'))

    def test_only_same_requester_date_completed_known_actuals_including_zero(self):
        self.make_claim('500', '400', status=ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW)
        self.make_claim('500', '400', status=ClaimRequest.Status.CANCELLED)
        self.make_claim('500', None)
        self.make_claim('500', '0')
        self.make_claim('500', '400', request_date=date(2026, 10, 23))
        other = User.objects.create_user(username='actual-day-other')
        self.make_claim('500', '400', requester=other)
        self.assertEqual(self.detail().context['claim'].completed_daily_actual_total, Decimal('5030'))
        self.claim.status = self.previous.status = ClaimRequest.Status.PENDING_ACCOUNTANT_REVIEW
        self.claim.save(update_fields=['status'])
        self.previous.save(update_fields=['status'])
        self.assertEqual(self.detail().context['claim'].completed_daily_actual_total, Decimal('0'))

    def test_actual_total_is_live_but_original_snapshot_never_changes(self):
        self.previous.actual_total = Decimal('60')
        self.previous.save(update_fields=['actual_total'])
        self.assertEqual(self.detail().context['claim'].completed_daily_actual_total, Decimal('5010'))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.daily_requested_total, Decimal('5050'))

    def test_actor_without_actual_visibility_does_not_receive_aggregate(self):
        self.claim.approval_records.create(actor=self.supervisor, action=ClaimApproval.Action.APPROVED,
            status_before=ClaimRequest.Status.PENDING_SUPERVISOR,
            status_after=ClaimRequest.Status.PENDING_ACCOUNTING)
        self.client.force_login(self.supervisor)
        response = self.detail()
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'ยอดใช้จริงสะสมของวันที่ขอเบิก')
        self.assertIsNone(response.context['claim'].completed_daily_actual_total)
