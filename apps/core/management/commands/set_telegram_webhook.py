from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.telegram import telegram_api_request


class Command(BaseCommand):
    help = "Register the Petty Cash Telegram callback webhook."

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN is not configured.")
        if not settings.PUBLIC_BASE_URL.startswith("https://"):
            raise CommandError("PUBLIC_BASE_URL must be a public https:// URL.")
        if not settings.TELEGRAM_WEBHOOK_SECRET:
            raise CommandError("TELEGRAM_WEBHOOK_SECRET is not configured.")

        webhook_url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/telegram/webhook/"
        configured = telegram_api_request(
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": settings.TELEGRAM_WEBHOOK_SECRET,
                "allowed_updates": ["callback_query"],
            },
        )
        if not configured:
            raise CommandError("Telegram did not accept the webhook configuration.")
        self.stdout.write(self.style.SUCCESS(f"Telegram webhook configured: {webhook_url}"))
