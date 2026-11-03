from django.db import migrations, models


def preserve_finished_excess_routes(apps, schema_editor):
    Claim = apps.get_model('claims', 'ClaimRequest')
    Claim.objects.using(schema_editor.connection.alias).filter(
        actual_total__gt=models.F('requested_total'),
    ).exclude(status='PENDING_EXCESS_APPROVAL').update(excess_cfo_required=False)


class Migration(migrations.Migration):
    dependencies = [('claims', '0021_claimapproval_actual_corrected')]
    operations = [
        migrations.AddField(model_name='claimrequest', name='daily_requested_total', field=models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)),
        migrations.AddField(model_name='claimrequest', name='excess_daily_total', field=models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)),
        migrations.AddField(model_name='claimrequest', name='excess_cfo_required', field=models.BooleanField(null=True, blank=True)),
        migrations.AddField(model_name='claimrequest', name='excess_accounting_approved_at', field=models.DateTimeField(null=True, blank=True)),
        migrations.AddField(model_name='claimrequest', name='excess_cfo_approved_at', field=models.DateTimeField(null=True, blank=True)),
        migrations.AddIndex(model_name='claimrequest', index=models.Index(fields=['requester', 'request_date'], name='claim_requester_date_idx')),
        migrations.RunPython(preserve_finished_excess_routes, migrations.RunPython.noop),
    ]
