from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("claims", "0005_claimattachment")]

    operations = [
        migrations.AlterField(
            model_name="claimapproval",
            name="action",
            field=models.CharField(
                choices=[
                    ("SUBMITTED", "ส่งอนุมัติ"),
                    ("APPROVED", "อนุมัติ"),
                    ("RETURNED", "ส่งกลับแก้ไข"),
                    ("REJECTED", "ไม่อนุมัติ"),
                    ("PAID", "บันทึกจ่ายเงิน"),
                ],
                max_length=20,
            ),
        ),
    ]
