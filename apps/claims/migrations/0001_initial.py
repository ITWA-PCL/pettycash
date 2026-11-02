# Generated manually for the Petty Cash claims module.
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [("accounts", "0002_create_business_roles")]

    operations = [
        migrations.CreateModel(
            name="ClaimRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document_number", models.CharField(blank=True, max_length=20, null=True, unique=True)),
                ("department", models.CharField(blank=True, max_length=150)),
                ("work_location", models.CharField(max_length=10)),
                ("request_date", models.DateField(default=django.utils.timezone.localdate)),
                ("budget", models.CharField(blank=True, max_length=200)),
                ("purpose", models.TextField()),
                ("requested_total", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("actual_total", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("status", models.CharField(choices=[("DRAFT", "แบบร่าง"), ("PENDING_SUPERVISOR", "รอหัวหน้าอนุมัติ"), ("PENDING_ACCOUNTING", "รอผู้อนุมัติบัญชี"), ("PENDING_PAYMENT", "รอจ่ายเงิน"), ("PAID", "จ่ายเงินแล้ว"), ("PENDING_CLEARANCE", "รอเคลียร์"), ("COMPLETED", "เสร็จสิ้น"), ("RETURNED", "ส่งกลับแก้ไข"), ("REJECTED", "ไม่อนุมัติ"), ("CANCELLED", "ยกเลิก")], default="DRAFT", max_length=30)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("requester", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="claim_requests", to=settings.AUTH_USER_MODEL)),
                ("supervisor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supervised_claim_requests", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-updated_at",)},
        ),
        migrations.CreateModel(
            name="ClaimItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("description", models.CharField(max_length=500)),
                ("requested_amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("actual_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("claim_request", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="claims.claimrequest")),
            ],
            options={"ordering": ("sort_order", "pk")},
        ),
    ]
