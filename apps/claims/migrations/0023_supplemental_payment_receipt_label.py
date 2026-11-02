from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('claims', '0022_daily_totals_and_excess_cfo')]
    operations = [
        migrations.AlterField(
            model_name='claimapproval',
            name='action',
            field=models.CharField(max_length=20, choices=[
                ('SUBMITTED', 'ส่งอนุมัติ'),
                ('APPROVED', 'อนุมัติ'),
                ('ACKNOWLEDGED', 'รับทราบ'),
                ('RETURNED', 'ส่งกลับแก้ไข'),
                ('REJECTED', 'ไม่อนุมัติ'),
                ('PAID', 'บันทึกจ่ายเงิน'),
                ('RECEIVED', 'ยืนยันรับเงิน'),
                ('AUTO_RECEIVED', 'ระบบถือว่ารับเงินอัตโนมัติ'),
                ('COMPLETED', 'ปิดรายการ'),
                ('EXCESS_SUBMITTED', 'ส่งอนุมัติส่วนต่าง'),
                ('ACTUAL_CORRECTED', 'บัญชีแก้ยอดใช้จริง'),
                ('SUPPLEMENTAL_PAID', 'จ่ายเงินส่วนต่าง'),
                ('CANCELLED', 'ยกเลิกคำขอ (รับเงินคืนครบ)'),
            ]),
        ),
    ]
