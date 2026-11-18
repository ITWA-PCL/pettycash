from django.contrib import admin
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import AdminPasswordChangeForm, AdminUserCreationForm
from django.core.exceptions import ValidationError

from apps.core.audit import log_event, record_transaction_log
from .models import User
from .password_policy import apply_local_password, invalidate_user_sessions, validate_local_password


class PettyCashAdminUserCreationForm(AdminUserCreationForm):
    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("username",)

    def validate_password_for_user(self, user, **kwargs):
        if not self.cleaned_data.get("set_usable_password", True):
            return
        password = self.cleaned_data.get("password2")
        if password and getattr(user, "is_local_account", True):
            try:
                validate_local_password(
                    password,
                    user=user,
                    username=user.username,
                    forbid_current_password=False,
                )
            except ValidationError as error:
                self.add_error("password2", error)
                return
        super().validate_password_for_user(user, **kwargs)

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get("set_usable_password", True):
            apply_local_password(user, self.cleaned_data["password1"], mark_must_change=False)
        else:
            user.set_unusable_password()
            user.must_change_password = False
        if commit:
            user.save()
            if hasattr(self, "save_m2m"):
                self.save_m2m()
        return user


class PettyCashAdminPasswordChangeForm(AdminPasswordChangeForm):
    def validate_password_for_user(self, user, **kwargs):
        if not self.cleaned_data.get("set_usable_password", True):
            return
        password = self.cleaned_data.get("password2")
        if password and getattr(user, "is_local_account", True):
            try:
                validate_local_password(
                    password,
                    user=user,
                    username=user.username,
                    forbid_current_password=True,
                )
            except ValidationError as error:
                self.add_error("password2", error)
                return
        super().validate_password_for_user(user, **kwargs)

    def save(self, commit=True):
        if self.cleaned_data.get("set_usable_password", True):
            is_local = getattr(self.user, "is_local_account", True)
            apply_local_password(
                self.user,
                self.cleaned_data["password1"],
                mark_must_change=is_local,
            )
            if not is_local:
                self.user.must_change_password = False
        else:
            self.user.set_unusable_password()
            self.user.must_change_password = False
        if commit:
            self.user.save()
        return self.user


@admin.register(User)
class PettyCashUserAdmin(UserAdmin):
    add_form = PettyCashAdminUserCreationForm
    change_password_form = PettyCashAdminPasswordChangeForm
    readonly_fields = UserAdmin.readonly_fields + ("password_changed_at",)
    fieldsets = UserAdmin.fieldsets + (
        (
            "ข้อมูลงานและการเชื่อมต่อ",
            {
                "fields": (
                    "auth_source",
                    "is_ad_auth",
                    "password_changed_at",
                    "must_change_password",
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

    def save_model(self, request, obj, form, change):
        if not obj.is_local_account:
            obj.must_change_password = False
        super().save_model(request, obj, form, change)

    def user_change_password(self, request, id, form_url=""):
        response = super().user_change_password(request, id, form_url=form_url)
        if request.method == "POST" and response.status_code == 302:
            user = self.get_object(request, unquote(id))
            if user is not None:
                exclude_key = request.session.session_key if user.pk == request.user.pk else None
                invalidate_user_sessions(user, exclude_session_key=exclude_key)
                log_event(
                    request.user,
                    "ADMIN_RESET_USER_PASSWORD",
                    user.username,
                    "รีเซ็ต/เปลี่ยนรหัสผ่านผ่าน Django Admin",
                )
                record_transaction_log(
                    category="password_policy",
                    action="django_admin_password_change",
                    status="success",
                    message=f"ผู้ดูแล '{request.user.username}' เปลี่ยนรหัสผ่านให้ '{user.username}' ผ่าน Django Admin",
                    details={
                        "username": user.username,
                        "admin": request.user.username,
                        "must_change_password": user.must_change_password,
                        "has_usable_password": user.has_usable_password(),
                        "source": "django_admin",
                    },
                    triggered_by=f"user:{request.user.username}",
                )
        return response
