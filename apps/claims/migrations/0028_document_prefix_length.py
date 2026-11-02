from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('claims', '0027_exposure_references_and_monthly_numbers'), ('core', '0025_location_document_prefixes')]
    operations = [
        migrations.AlterField(model_name='claimdocumentsequence', name='prefix', field=models.CharField(max_length=12)),
        migrations.AlterField(model_name='claimrequest', name='document_number', field=models.CharField(blank=True, max_length=32, null=True, unique=True)),
    ]
