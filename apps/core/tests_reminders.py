from io import StringIO
from datetime import time
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.claims.models import ClaimRequest
from apps.core.models import DailyReminderRun
from apps.core.system_settings import get_system_settings


class DailyPendingReminderTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(username="requester", password="safe-test-password", telegram_user_id="1001", telegram_notifications_enabled=True)
        self.supervisor = User.objects.create_user(username="supervisor", password="safe-test-password", telegram_user_id="1002", telegram_notifications_enabled=True)
        self.accountant = User.objects.create_user(username="accountant", password="safe-test-password", telegram_user_id="1003", telegram_notifications_enabled=True)

    def make_claim(self, status, number):
        return ClaimRequest.objects.create(
            document_number=number, requester=self.requester, supervisor=self.supervisor,
            accounting_manager=self.supervisor, accountant=self.accountant, business_unit="IT",
            department="IT", work_location=User.WorkLocation.HEAD_OFFICE, budget="Office",
            purpose="ทดสอบแจ้งเตือน", status=status,
        )

    @patch("apps.core.management.commands.send_daily_pending_reminders.send_telegram_group_notification", return_value=True)
    @patch("apps.core.management.commands.send_daily_pending_reminders.send_telegram_notification", return_value=True)
    def test_sends_one_daily_summary_per_responsible_user_and_group(self, direct, group):
        self.make_claim(ClaimRequest.Status.DRAFT, "BR-001")
        self.make_claim(ClaimRequest.Status.PENDING_SUPERVISOR, "BR-002")
        self.make_claim(ClaimRequest.Status.PAID, "BR-003")
        self.make_claim(ClaimRequest.Status.COMPLETED, "BR-004")

        call_command("send_daily_pending_reminders", "--force", stdout=StringIO())

        self.assertEqual(direct.call_count, 2)
        self.assertEqual({call.args[0].username for call in direct.call_args_list}, {"requester", "supervisor"})
        group.assert_called_once()
        self.assertIn("3 รายการ", group.call_args.args[1])
        self.assertNotIn("เสร็จสิ้น", group.call_args.args[1])

    @patch("apps.core.management.commands.send_daily_pending_reminders.send_telegram_group_notification")
    @patch("apps.core.management.commands.send_daily_pending_reminders.send_telegram_notification")
    def test_scheduled_run_is_recorded_and_not_sent_twice(self, direct, group):
        self.make_claim(ClaimRequest.Status.PENDING_PAYMENT, "BR-005")
        configuration = get_system_settings()
        configuration.daily_reminder_time = time(0, 0)
        configuration.save(update_fields=["daily_reminder_time"])

        call_command("send_daily_pending_reminders", "--if-due", stdout=StringIO())
        call_command("send_daily_pending_reminders", "--if-due", stdout=StringIO())

        self.assertEqual(DailyReminderRun.objects.filter(run_date=timezone.localdate()).count(), 1)
        self.assertEqual(direct.call_count, 1)
        self.assertEqual(group.call_count, 1)
