from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import migrations, models


def record_accepted_dates(apps, schema_editor):
    Claim = apps.get_model('claims', 'ClaimRequest')
    Approval = apps.get_model('claims', 'ClaimApproval')
    for claim in Claim.objects.all().iterator():
        submitted = Approval.objects.filter(claim_request_id=claim.pk, action='SUBMITTED').order_by('-pk').first()
        if submitted:
            submitted_date = submitted.created_at.astimezone(ZoneInfo('Asia/Bangkok')).date()
            if claim.request_date >= submitted_date - timedelta(days=5):
                Claim.objects.filter(pk=claim.pk).update(accepted_request_date=claim.request_date)


class Migration(migrations.Migration):
    dependencies = [('claims', '0026_telegram_approval_safety')]
    operations = [
        migrations.AddField(model_name='claimrequest', name='approval_exposure_details', field=models.JSONField(blank=True, null=True)),
        migrations.AddField(model_name='claimrequest', name='excess_exposure_details', field=models.JSONField(blank=True, null=True)),
        migrations.AddField(model_name='claimrequest', name='accepted_request_date', field=models.DateField(blank=True, null=True)),
        migrations.CreateModel(name='ClaimDocumentSequence', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('prefix', models.CharField(max_length=2)),
            ('period', models.CharField(max_length=6)),
            ('last_number', models.PositiveIntegerField(default=0)),
        ], options={'constraints': [models.UniqueConstraint(fields=('prefix', 'period'), name='unique_claim_monthly_sequence')]}),
        migrations.RunPython(record_accepted_dates, migrations.RunPython.noop),
    ]
