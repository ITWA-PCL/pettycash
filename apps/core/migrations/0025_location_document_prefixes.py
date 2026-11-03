from django.core.validators import RegexValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0024_petty_receipt_three_days')]
    operations = [
        migrations.AddField(model_name='systemsettings', name='head_office_document_prefix',
            field=models.CharField(default='HO', max_length=12, validators=[RegexValidator(r'^[A-Za-z0-9]+$', 'ใช้ได้เฉพาะตัวอักษรภาษาอังกฤษและตัวเลข')])),
        migrations.AddField(model_name='systemsettings', name='branch_document_prefix',
            field=models.CharField(default='BR', max_length=12, validators=[RegexValidator(r'^[A-Za-z0-9]+$', 'ใช้ได้เฉพาะตัวอักษรภาษาอังกฤษและตัวเลข')])),
    ]
