from django.db import migrations
from apps.core.secret_fields import SecretTextField


def encrypt_existing_ad_secret(apps, schema_editor):
    model = apps.get_model('core', 'SystemSettings')
    for instance in model.objects.using(schema_editor.connection.alias).exclude(ad_secret_key=''):
        instance.save(update_fields=['ad_secret_key'], using=schema_editor.connection.alias)


def decrypt_existing_ad_secret(apps, schema_editor):
    model = apps.get_model('core', 'SystemSettings')
    for instance in model.objects.using(schema_editor.connection.alias).exclude(ad_secret_key=''):
        with schema_editor.connection.cursor() as cursor:
            cursor.execute('UPDATE core_systemsettings SET ad_secret_key = %s WHERE id = %s', [instance.ad_secret_key, instance.pk])


class Migration(migrations.Migration):
    dependencies = [('core', '0025_location_document_prefixes')]
    operations = [
        migrations.AddField(model_name='systemsettings', name='telegram_bot_token', field=SecretTextField(blank=True)),
        migrations.AddField(model_name='systemsettings', name='telegram_webhook_secret', field=SecretTextField(blank=True)),
        migrations.AlterField(model_name='systemsettings', name='ad_secret_key', field=SecretTextField(blank=True)),
        migrations.RunPython(encrypt_existing_ad_secret, decrypt_existing_ad_secret),
    ]
