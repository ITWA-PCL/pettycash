from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("accounts", "0010_user_theme_preference")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="work_location_other",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AlterField(
            model_name="user",
            name="work_location",
            field=models.CharField(choices=[("HQ", "สำนักงานใหญ่"), ("BRANCH", "กาญจนบุรี"), ("OTHER", "อื่นๆ")], default="HQ", max_length=10),
        ),
    ]
