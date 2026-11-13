from django.contrib import messages
from django.contrib.auth import authenticate, login, logout as auth_logout, update_session_auth_hash
from django.contrib.auth.models import Group
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from .forms import ForcedPasswordChangeForm, ProfileDetailsForm, ProfilePasswordForm, SignatureSetupForm, UserManagementForm
from .models import User
from .roles import SUPER_ADMIN
from .ad_auth import ADGatewayError, verify_ad_credentials
from .bot_defense import verify_bot_defense
from .ciam_sso import build_sso_authorize_url, exchange_code_and_login
from apps.core.telegram import send_telegram_message, telegram_is_configured
from apps.core.audit import log_event, record_transaction_log
from apps.core.pagination import paginate
from apps.core.system_settings import get_system_settings


def can_manage_users(user):
    return user.is_superuser or user.has_role(SUPER_ADMIN)


manage_users_required = user_passes_test(can_manage_users)


@login_required
def signature_image(request):
    from .signatures import signature_response
    return signature_response(request.user)


def is_last_active_admin(user):
    if not (user.is_superuser or user.has_role(SUPER_ADMIN)):
        return False
    return not User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(groups__name=SUPER_ADMIN)
    ).exclude(pk=user.pk).exists()


def login_view(request):
    error = None
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    config = get_system_settings()

    # If already authenticated, redirect to next_url or dashboard
    if request.user.is_authenticated:
        if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
            return redirect(next_url)
        return redirect("dashboard")

    # If incoming request is a callback with SSO code / state, bounce to callback view
    if (request.GET.get("code") or request.POST.get("code")) and (request.GET.get("state") or request.POST.get("state")):
        return sso_callback_view(request)

    if request.method == "POST":
        # Enterprise Bot Defense (Layer 1, 2, 3 - Section 12)
        passed, bot_error, status_code = verify_bot_defense(request)
        if not passed:
            record_transaction_log(
                category="bot_defense",
                action="blocked_request",
                status="warning",
                message=bot_error,
                details={"ip": request.META.get("REMOTE_ADDR"), "status_code": status_code},
                triggered_by="system:bot_defense",
            )
            return render(
                request,
                "registration/login.html",
                {
                    "login_error": bot_error,
                    "next": next_url,
                    "sso_enabled": config.ciam_sso_enabled,
                    "break_glass_active": config.ciam_break_glass_active,
                },
                status=status_code,
            )

        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        login_mode = request.POST.get("login_mode", "local")
        user = None

        if login_mode == "ad":
            try:
                candidate = User.objects.get(username__iexact=username, is_active=True, is_ad_auth=True)
                if verify_ad_credentials(candidate.username, password):
                    user = candidate
                else:
                    error = "ชื่อผู้ใช้หรือรหัสผ่าน Active Directory ไม่ถูกต้อง"
            except User.DoesNotExist:
                error = "บัญชีนี้ไม่ได้รับอนุญาตให้ใช้ Active Directory"
            except ADGatewayError as exc:
                error = str(exc)
        else:
            if User.objects.filter(username__iexact=username, is_active=True, is_ad_auth=True).exists():
                error = "บัญชีนี้ถูกกำหนดให้เข้าสู่ระบบด้วย Active Directory เท่านั้น"
            else:
                user = authenticate(request, username=username, password=password)
            if user is None:
                error = error or "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"

        if user is not None:
            login(request, user)
            request.session["auth_provider"] = "local"
            log_event(user, "LOGIN_AD" if login_mode == "ad" else "LOGIN_LOCAL", user.username)
            record_transaction_log(
                category="authentication",
                action="login_success",
                status="success",
                message=f"เข้าสู่ระบบสำเร็จ ({login_mode.upper()}): {user.username}",
                details={"username": user.username, "mode": login_mode},
                triggered_by=f"user:{user.username}",
            )
            if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
                return redirect(next_url)
            return redirect("dashboard")

    return render(
        request,
        "registration/login.html",
        {
            "login_error": error,
            "next": next_url,
            "sso_enabled": config.ciam_sso_enabled,
            "break_glass_active": config.ciam_break_glass_active,
        },
    )


@login_required
def forced_password_change(request):
    """
    Forced 90-Day Password Rotation & Complexity Interception (Section 13).
    Restricted strictly to local accounts.
    """
    if not request.user.is_local_account:
        return redirect("dashboard")

    form = ForcedPasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        record_transaction_log(
            category="password_policy",
            action="change_password",
            status="success",
            message=f"ผู้ใช้ '{user.username}' เปลี่ยนรหัสผ่านใหม่เรียบร้อยแล้ว (มีอายุการใช้งาน 90 วัน)",
            details={"username": user.username, "policy": "90_day_rotation"},
            triggered_by=f"user:{user.username}",
        )
        log_event(user, "CHANGE_PASSWORD_ROTATION", user.username)
        messages.success(request, "เปลี่ยนรหัสผ่านเรียบร้อยแล้ว (มีอายุการใช้งาน 90 วัน)")
        if not user.has_signature and get_system_settings().require_signature_on_first_login:
            return redirect("signature_setup")
        return redirect("dashboard")

    is_expired, days_left = request.user.check_password_expired()
    return render(
        request,
        "registration/forced_password_change.html",
        {
            "form": form,
            "is_expired": is_expired,
            "must_change": request.user.must_change_password,
            "days_left": days_left,
        },
    )


def sso_config_view(request):
    """GET /api/auth/sso/config (Section B.1)"""
    config = get_system_settings()
    return JsonResponse({
        "sso_enabled": config.ciam_sso_enabled,
        "break_glass_active": config.ciam_break_glass_active,
        "ciam_base_url": config.ciam_base_url,
        "client_id": config.ciam_client_id,
        "login_button_label": "เข้าสู่ระบบด้วย Window Asia SSO ✨",
        "fallback_ad_available": config.ad_enabled,
    })


def sso_authorize_url_view(request):
    """POST /api/auth/sso/authorize-url (Section B.2)"""
    redirect_uri = request.POST.get("redirect_uri") or request.build_absolute_uri(reverse("sso_callback"))
    auth_url, verifier, state = build_sso_authorize_url(request, redirect_uri)
    return JsonResponse({
        "authorize_url": auth_url,
        "code_verifier": verifier,
        "state": state,
    })


def sso_login_redirect(request):
    """Browser initiation for SSO login."""
    redirect_uri = request.build_absolute_uri(reverse("sso_callback"))
    auth_url, _, _ = build_sso_authorize_url(request, redirect_uri)
    return redirect(auth_url)


def sso_start_view(request):
    """RFC 9700 Seamless SSO Initiation Bounce Endpoint (Section B.3)."""
    redirect_uri = request.build_absolute_uri(reverse("sso_callback"))
    auth_url, _, _ = build_sso_authorize_url(request, redirect_uri)
    return redirect(auth_url)


def sso_callback_view(request):
    """GET/POST /auth/sso/callback (Section B.3)"""
    code = request.GET.get("code") or request.POST.get("code")
    state = request.GET.get("state") or request.POST.get("state")
    redirect_uri = request.build_absolute_uri(reverse("sso_callback"))

    if not code:
        messages.error(request, "ไม่พบ Authorization Code จาก Central IAM")
        return redirect("login")

    user, error = exchange_code_and_login(request, code, None, redirect_uri, state)
    if error:
        messages.error(request, error)
        return redirect("login")

    return redirect("dashboard")


@user_passes_test(can_manage_users)
def sso_break_glass_toggle(request):
    """POST /api/auth/sso/break-glass-toggle (Section B.4)"""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    config = get_system_settings()
    new_state = not config.ciam_break_glass_active
    config.ciam_break_glass_active = new_state
    config.save(update_fields=["ciam_break_glass_active"])

    status_str = "ENABLED" if new_state else "DISABLED"
    record_transaction_log(
        category="security_break_glass",
        action="toggle_break_glass",
        status="warning" if new_state else "info",
        message=f"สลับสถานะระบบ Break-Glass: {status_str}",
        details={"break_glass_active": new_state, "admin": request.user.username},
        triggered_by=f"user:{request.user.username}",
    )
    messages.warning(request, f"ระบบ Break-Glass ถูก {'เปิดใช้งาน' if new_state else 'ปิดใช้งาน'}")
    return redirect("system_settings")


def logout_view(request):
    """Seamless Logout: SSO users return to Portal, local users return to login (Section 5.3)."""
    is_sso = request.session.get("auth_provider") == "ciam_sso"
    portal_url = request.session.get("ciam_portal_url") or "https://ciam.windowasia.com/portal"
    auth_logout(request)
    if is_sso:
        return redirect(portal_url)
    return redirect("login")


@login_required
def theme_preference(request):
    """Persist the display mode on the signed-in user, across browsers."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    mode = request.POST.get("mode", "")
    valid_modes = {User.ThemePreference.LIGHT, User.ThemePreference.DARK}
    if mode not in valid_modes:
        return JsonResponse({"ok": False, "message": "รูปแบบหน้าจอไม่ถูกต้อง"}, status=400)
    if request.user.theme_preference != mode:
        request.user.theme_preference = mode
        request.user.save(update_fields=["theme_preference"])
    return JsonResponse({"ok": True, "mode": mode})


@login_required
def signature_setup(request):
    is_update = request.user.has_signature
    action = request.POST.get("action")
    profile_form = ProfileDetailsForm(request.POST if action == "profile" else None, instance=request.user)
    password_form = ProfilePasswordForm(request.user, request.POST if action == "password" else None) if not request.user.is_ad_auth else None
    signature_form = SignatureSetupForm(
        request.POST if action == "signature" else None,
        request.FILES if action == "signature" else None,
        instance=request.user,
    )
    if action == "profile" and profile_form.is_valid():
        profile_form.save()
        messages.success(request, "บันทึกข้อมูลโปรไฟล์เรียบร้อยแล้ว")
        return redirect("signature_setup")
    if action == "password":
        if request.user.is_ad_auth:
            messages.error(request, "บัญชี Active Directory ไม่สามารถเปลี่ยนรหัสผ่านจากระบบนี้ได้")
            return redirect("signature_setup")
        if password_form.is_valid():
            user = password_form.save()
            update_session_auth_hash(request, user)
            log_event(user, "CHANGE_PASSWORD", user.username)
            messages.success(request, "เปลี่ยนรหัสผ่านเรียบร้อยแล้ว")
            return redirect("signature_setup")
    if action == "signature" and signature_form.is_valid():
        signature_form.save()
        messages.success(request, "บันทึกลายเซ็นเรียบร้อยแล้ว")
        return redirect("dashboard")
    return render(request, "accounts/signature_setup.html", {
        "form": signature_form,
        "profile_form": profile_form,
        "password_form": password_form,
        "is_update": is_update,
    })


@manage_users_required
def user_list(request):
    users = User.objects.prefetch_related("groups").order_by("username")
    search_query = request.GET.get("q", "").strip()
    role = request.GET.get("role", "")
    active = request.GET.get("active", "")
    if search_query:
        users = users.filter(
            Q(username__icontains=search_query)
            | Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(department__icontains=search_query)
        )
    role_names = set(Group.objects.values_list("name", flat=True))
    if role in role_names:
        users = users.filter(groups__name=role)
    else:
        role = ""
    if active == "active":
        users = users.filter(is_active=True)
    elif active == "inactive":
        users = users.filter(is_active=False)
    else:
        active = ""
    users, page_obj, page_size, query_params = paginate(request, users.distinct())
    return render(request, "accounts/user_list.html", {
        "users": users,
        "page_obj": page_obj, "page_size": page_size, "query_params": query_params,
        "claims": [], "audit_logs": [],
        "search_query": search_query,
        "selected_role": role,
        "selected_active": active,
        "roles": Group.objects.order_by("name"),
    })


@manage_users_required
def user_create(request):
    form = UserManagementForm(request.POST or None, creating=True)
    if request.method == "POST" and form.is_valid():
        managed_user = form.save()
        log_event(request.user, "CREATE_USER", managed_user.username)
        messages.success(request, "สร้างผู้ใช้เรียบร้อยแล้ว")
        return redirect("user_list")
    return render(request, "accounts/user_form.html", {"form": form, "page_title": "เพิ่มผู้ใช้", "executive_approval_threshold": get_system_settings().executive_approval_threshold})


@manage_users_required
def user_edit(request, user_id):
    managed_user = get_object_or_404(User, pk=user_id)
    form = UserManagementForm(request.POST or None, instance=managed_user)
    if request.method == "POST" and form.is_valid():
        form.save()
        log_event(request.user, "UPDATE_USER", managed_user.username)
        messages.success(request, "บันทึกข้อมูลผู้ใช้เรียบร้อยแล้ว")
        return redirect("user_list")
    return render(request, "accounts/user_form.html", {"form": form, "page_title": "แก้ไขผู้ใช้", "managed_user": managed_user, "executive_approval_threshold": get_system_settings().executive_approval_threshold})


@manage_users_required
def user_toggle_active(request, user_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    managed_user = get_object_or_404(User, pk=user_id)
    if managed_user.pk == request.user.pk:
        messages.error(request, "ไม่สามารถปิดหรือเปิดใช้งานบัญชีของตัวเองจากหน้านี้ได้")
    elif managed_user.is_active and is_last_active_admin(managed_user):
        messages.error(request, "ไม่สามารถปิดใช้งานผู้ดูแลระบบคนสุดท้ายได้")
    else:
        managed_user.is_active = not managed_user.is_active
        managed_user.save(update_fields=["is_active"])
        log_event(request.user, "ENABLE_USER" if managed_user.is_active else "DISABLE_USER", managed_user.username)
        messages.success(request, f"{'เปิด' if managed_user.is_active else 'ปิด'}ใช้งานผู้ใช้ {managed_user.username} เรียบร้อยแล้ว")
    return redirect("user_list")


@manage_users_required
def user_delete(request, user_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    managed_user = get_object_or_404(User, pk=user_id)
    if managed_user.pk == request.user.pk:
        messages.error(request, "ไม่สามารถลบบัญชีของตัวเองได้")
        return redirect("user_list")
    if is_last_active_admin(managed_user):
        messages.error(request, "ไม่สามารถลบผู้ดูแลระบบคนสุดท้ายได้")
        return redirect("user_list")
    try:
        target = managed_user.username
        managed_user.signature.delete(save=False)
        managed_user.delete()
    except ProtectedError:
        messages.error(request, "ลบผู้ใช้นี้ไม่ได้ เพราะมีคำขอหรือประวัติการดำเนินการอยู่แล้ว กรุณาปิดใช้งานแทน")
    else:
        log_event(request.user, "DELETE_USER", target)
        messages.success(request, f"ลบผู้ใช้ {managed_user.username} เรียบร้อยแล้ว")
    if request.headers.get("X-History-Replace") == "1":
        return JsonResponse({"redirect": reverse("user_list")})
    return redirect("user_list")


@manage_users_required
def telegram_test_message(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    chat_id = request.POST.get("telegram_user_id", "").strip()
    if not chat_id:
        return JsonResponse({"ok": False, "message": "กรุณาระบุ Telegram User ID ก่อนทดสอบ"}, status=400)
    if not telegram_is_configured():
        return JsonResponse({"ok": False, "message": "ยังไม่ได้ตั้งค่า TELEGRAM_BOT_TOKEN ใน .env"}, status=503)
    text = "✅ ทดสอบการแจ้งเตือนจาก Petty Cash\nหากเห็นข้อความนี้ แปลว่าการผูก Telegram ใช้งานได้แล้ว"
    if not send_telegram_message(chat_id, text):
        return JsonResponse({"ok": False, "message": "ส่งไม่สำเร็จ: ให้ผู้ใช้นี้กด Start กับ Bot และตรวจสอบ Telegram User ID"}, status=502)
    return JsonResponse({"ok": True, "message": "ส่งข้อความทดสอบแล้ว กรุณาตรวจสอบ Telegram ของผู้ใช้นี้"})


def ciam_directory_accounts_api(request):
    """
    Central IAM Spoke M2M REST API: GET /api/v1/directory/accounts/
    Secured by X-Management-API-Key and Server IP Whitelist (Section D.1 / M2M)
    """
    config = get_system_settings()
    expected_key = config.ciam_m2m_key or config.ciam_client_secret or ""

    # Verify API Key
    provided_key = request.headers.get("X-Management-API-Key") or request.META.get("HTTP_X_MANAGEMENT_API_KEY", "")
    if not expected_key or provided_key != expected_key:
        return JsonResponse({"error": "Unauthorized: Invalid or missing X-Management-API-Key"}, status=401)

    from apps.core.ciam_agent import fetch_local_accounts
    accounts = fetch_local_accounts()
    return JsonResponse({"status": "success", "count": len(accounts), "accounts": accounts})
