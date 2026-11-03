from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import AdminAuditLog, Notification
from apps.core.system_settings import get_system_settings


class Command(BaseCommand):
    help = "Delete audit logs and in-app notifications older than the configured retention period."

    def handle(self, *args, **options):
        retention_days = get_system_settings().log_retention_days
        if retention_days < 1:
            raise ValueError("log_retention_days must be at least 1")

        cutoff = timezone.now() - timedelta(days=retention_days)
        audit_deleted, _ = AdminAuditLog.objects.filter(created_at__lt=cutoff).delete()
        notification_deleted, _ = Notification.objects.filter(created_at__lt=cutoff).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Removed {audit_deleted} audit log(s) and {notification_deleted} notification(s) older than {retention_days} days."
            )
        )
