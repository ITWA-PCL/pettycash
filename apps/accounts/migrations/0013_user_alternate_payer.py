from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('accounts', '0012_user_position_level')]
    operations = [migrations.AddField(
        model_name='user', name='alternate_payer',
        field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
            related_name='alternate_payment_requesters', to=settings.AUTH_USER_MODEL,
            verbose_name='ผู้จ่ายสำรองเมื่อเบิกเอง',
            limit_choices_to={'is_active': True, 'groups__name': 'เจ้าหน้าที่บัญชี'}),
    )]
