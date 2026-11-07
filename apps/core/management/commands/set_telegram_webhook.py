from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.telegram import telegram_api_request
from apps.core.integrations import public_base_url, telegram_token, telegram_secret


class Command(BaseCommand):
    help = "Register the Petty Cash Telegram callback webhook."

    def handle(self, *args, **options):
        if not telegram_token():
            raise CommandError("TELEGRAM_BOT_TOKEN is not configured.")
        if not public_base_url().startswith("https://"):
            raise CommandError("PUBLIC_BASE_URL must be a public https:// URL.")
        if not telegram_secret():
            raise CommandError("TELEGRAM_WEBHOOK_SECRET is not configured.")

        webhook_url = f"{public_base_url()}/telegram/webhook/"
        configured = telegram_api_request(
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": telegram_secret(),
                "allowed_updates": ["callback_query"],
            },
        )
        if not configured:
            raise CommandError("Telegram did not accept the webhook configuration.")
        self.stdout.write(self.style.SUCCESS(f"Telegram webhook configured: {webhook_url}"))
