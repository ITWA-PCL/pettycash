from django.db import migrations


ROLE_NAMES = (
    "ผู้ขอเบิก / ผู้รับเงิน",
    "หัวหน้าผู้ขอ",
    "ผู้อนุมัติบัญชี",
    "เจ้าหน้าที่บัญชี",
    "ผู้ดูแลระบบ",
    "ผู้บริหาร",
)


def create_business_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in ROLE_NAMES:
        Group.objects.get_or_create(name=name)


def remove_business_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=ROLE_NAMES).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [migrations.RunPython(create_business_roles, remove_business_roles)]
