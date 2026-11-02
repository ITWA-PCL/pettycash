import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.claims.models


class Migration(migrations.Migration):
    dependencies = [
        ("claims", "0019_budget_approvals"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ClaimPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("INITIAL", "จ่ายเงินตามคำขอ"), ("SUPPLEMENTAL", "จ่ายเงินส่วนต่าง")], max_length=12)),
                ("method", models.CharField(choices=[("TRANSFER", "โอนเงิน"), ("CASH", "เงินสด")], max_length=8)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("slip", models.FileField(blank=True, upload_to=apps.claims.models.payment_slip_upload_path)),
                ("slip_original_name", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("claim_request", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payments", to="claims.claimrequest")),
                ("paid_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recorded_claim_payments", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("created_at", "pk")},
        ),
    ]
