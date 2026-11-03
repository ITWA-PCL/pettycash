from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.claims.models import ClaimRequest


class DashboardTrackerTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username="tracker-requester", password="test-password")
        self.requester.signature.name = "signatures/test/tracker.png"
        self.requester.save(update_fields=["signature"])
        self.other = User.objects.create_user(username="tracker-other", password="test-password")
        self.supervisor = User.objects.create_user(username="tracker-supervisor", password="test-password")
        self.manager = User.objects.create_user(username="tracker-manager", password="test-password")
        self.accountant = User.objects.create_user(username="tracker-accountant", password="test-password")
        self.cfo = User.objects.create_user(username="tracker-cfo", password="test-password")
        self.client.force_login(self.requester)

    def make_claim(self, **overrides):
        values = {
            "requester": self.requester,
            "supervisor": self.supervisor,
            "accounting_manager": self.manager,
            "accountant": self.accountant,
            "cfo": self.cfo,
            "request_type": ClaimRequest.RequestType.ADVANCE,
            "status": ClaimRequest.Status.PENDING_SUPERVISOR,
            "work_location": "HQ",
            "purpose": "ติดตามขั้นตอน",
            "requested_total": Decimal("100.00"),
        }
        values.update(overrides)
        return ClaimRequest.objects.create(**values)

    def test_dashboard_shows_latest_active_request_and_steps_without_clicking(self):
        older = self.make_claim(status=ClaimRequest.Status.PENDING_ACCOUNTING)
        latest = self.make_claim(status=ClaimRequest.Status.PAID)
        self.make_claim(requester=self.other, purpose="private other claim")

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.context["featured_tracking"]["claim"], latest)
        self.assertEqual(response.context["more_tracking"][0]["claim"], older)
        self.assertContains(response, "ขั้นตอนปัจจุบัน")
        self.assertContains(response, "รอยืนยันรับเงิน")
        self.assertContains(response, "รับเงิน / ลงยอดจริง")
        self.assertNotContains(response, "private other claim")

    def test_refresh_endpoint_uses_current_status_and_only_own_requests(self):
        claim = self.make_claim(requested_total=Decimal("7000.00"))
        url = reverse("dashboard_claim_tracker")

        first = self.client.get(url)
        self.assertContains(first, "รอหัวหน้าอนุมัติ")
        self.assertEqual(first["Cache-Control"], "max-age=0, no-cache, no-store, must-revalidate, private")

        claim.status = ClaimRequest.Status.PENDING_ACCOUNTING
        claim.accounting_approved_at = timezone.now()
        claim.save(update_fields=["status", "accounting_approved_at", "updated_at"])
        refreshed = self.client.get(url)
        self.assertContains(refreshed, "รอ CFO อนุมัติ")
        self.assertContains(refreshed, self.cfo.username)
        self.assertNotContains(refreshed, self.other.username)

    def test_completed_request_does_not_show_tracker_card(self):
        self.make_claim(
            request_type=ClaimRequest.RequestType.PETTY_CASH,
            status=ClaimRequest.Status.COMPLETED,
        )
        response = self.client.get(reverse("dashboard"))
        self.assertIsNone(response.context["featured_tracking"])
        self.assertNotContains(response, "LIVE REQUEST TRACKING")
        self.assertContains(response, 'id="dashboard-tracker-region"')
        self.assertContains(response, ' hidden>')
        self.assertEqual(self.client.get(reverse("dashboard_claim_tracker")).content.strip(), b"")

    def test_petty_cash_waiting_for_receipt_stays_in_tracker(self):
        claim = self.make_claim(request_type=ClaimRequest.RequestType.PETTY_CASH, status=ClaimRequest.Status.PAID)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["featured_tracking"]["claim"], claim)
        self.assertContains(response, "รอยืนยันรับเงิน")
        self.assertContains(response, "รับเงิน")
        self.assertFalse(response.context["featured_tracking"]["is_terminal"])

    def test_tracker_disappears_after_last_active_request_is_completed(self):
        claim = self.make_claim()
        url = reverse("dashboard_claim_tracker")
        self.assertContains(self.client.get(url), "LIVE REQUEST TRACKING")

        claim.status = ClaimRequest.Status.COMPLETED
        claim.save(update_fields=["status", "updated_at"])
        self.assertEqual(self.client.get(url).content.strip(), b"")

    def test_refresh_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("dashboard_claim_tracker"))
        self.assertEqual(response.status_code, 302)
