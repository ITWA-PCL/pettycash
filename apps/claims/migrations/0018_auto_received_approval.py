import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("claims", "0017_optional_supervisor"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="claimapproval",
            name="actor",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="claim_approval_actions", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="claimapproval",
            name="action",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("SUBMITTED", "ส่งอนุมัติ"), ("APPROVED", "อนุมัติ"),
                    ("ACKNOWLEDGED", "รับทราบ"), ("RETURNED", "ส่งกลับแก้ไข"),
                    ("REJECTED", "ไม่อนุมัติ"), ("PAID", "บันทึกจ่ายเงิน"),
                    ("RECEIVED", "ยืนยันรับเงิน"),
                    ("AUTO_RECEIVED", "ระบบถือว่ารับเงินอัตโนมัติ"),
                    ("COMPLETED", "ปิดรายการ"),
                    ("EXCESS_SUBMITTED", "ส่งอนุมัติส่วนต่าง"),
                    ("SUPPLEMENTAL_PAID", "จ่ายเงินส่วนต่างและปิดรายการ"),
                    ("CANCELLED", "ยกเลิกคำขอ (รับเงินคืนครบ)"),
                ],
            ),
        ),
    ]
