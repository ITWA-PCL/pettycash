from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("claims", "0012_claimrequest_performance_indexes")]

    operations = [
        migrations.AlterField(
            model_name="claimrequest",
            name="work_location",
            field=models.CharField(choices=[("HQ", "สำนักงานใหญ่"), ("BRANCH", "กาญจนบุรี"), ("OTHER", "อื่นๆ")], max_length=10),
        ),
    ]
