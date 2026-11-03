from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.CreateModel(name="SystemSettings", fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")), ("company_name", models.CharField(default="บริษัท วินโดว์ เอเชีย จำกัด (มหาชน)", max_length=255)), ("executive_approval_threshold", models.DecimalField(decimal_places=2, default=5000, max_digits=12)), ("max_attachment_count", models.PositiveSmallIntegerField(default=5)), ("max_attachment_size_mb", models.PositiveSmallIntegerField(default=10)), ("updated_at", models.DateTimeField(auto_now=True))])]
