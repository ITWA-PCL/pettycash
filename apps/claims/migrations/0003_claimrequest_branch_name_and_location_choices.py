from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("claims", "0002_claimrequest_business_unit"), ("accounts", "0004_user_approval_flow_and_telegram")]

    operations = [
        migrations.AddField(model_name="claimrequest", name="branch_name", field=models.CharField(blank=True, max_length=150)),
        migrations.AlterField(model_name="claimrequest", name="work_location", field=models.CharField(choices=[("HQ", "สำนักงานใหญ่"), ("BRANCH", "สาขา")], max_length=10)),
    ]
