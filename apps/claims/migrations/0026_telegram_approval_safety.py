import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('claims', '0025_prepayment_correction'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='TelegramCallbackReceipt', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('callback_id', models.CharField(max_length=200, unique=True)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
        ]),
        migrations.CreateModel(name='TelegramApprovalPrompt', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('token', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
            ('context', models.CharField(max_length=160)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('actor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ('claim_request', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='telegram_approval_prompts', to='claims.claimrequest')),
        ], options={'constraints': [models.UniqueConstraint(fields=('claim_request', 'actor', 'context'), name='unique_telegram_approval_prompt')]}),
    ]
