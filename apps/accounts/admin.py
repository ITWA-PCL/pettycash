from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class PettyCashUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        (
            "ข้อมูลงานและการเชื่อมต่อ",
            {
                "fields": (
                    "auth_source",
                    "is_ad_auth",
                    "business_unit",
                    "department",
                    "work_location",
                    "supervisor",
                    "accounting_manager",
                    "accountant",
                    "alternate_payer",
                    "telegram_user_id",
                    "telegram_notifications_enabled",
                    "signature",
                    "signature_updated_at",
                    "onboarding_completed",
                )
            },
        ),
    )
    list_display = ("username", "get_full_name", "business_unit", "department", "work_location", "display_roles", "is_active", "is_staff")
    list_filter = ("work_location", "is_ad_auth", "groups", "is_active", "is_staff")
    search_fields = ("username", "first_name", "last_name", "email", "telegram_user_id")

    @admin.display(description="สิทธิ์")
    def display_roles(self, user):
        return ", ".join(user.role_names) or "-"
