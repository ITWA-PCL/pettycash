from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver


@receiver(user_logged_in, dispatch_uid='reset_telegram_reminder_on_login')
def reset_telegram_reminder(sender, request, user, **kwargs):
    if request is not None:
        request.session.pop('telegram_connection_reminder_dismissed', None)
