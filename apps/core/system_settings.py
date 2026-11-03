from .models import SystemSettings


def get_system_settings():
    configuration, _ = SystemSettings.objects.get_or_create(pk=1)
    return configuration
