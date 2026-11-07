from django.contrib import messages
from django.contrib.auth import authenticate, login, update_session_auth_hash
from django.contrib.auth.models import Group
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from .forms import ProfileDetailsForm, ProfilePasswordForm, SignatureSetupForm, UserManagementForm
from .models import User
from .roles import SUPER_ADMIN
from .ad_auth import ADGatewayError, verify_ad_credentials
from apps.core.telegram import send_telegram_message, telegram_is_configured
from apps.core.audit import log_event
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
    if request.method == "POST":
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
            log_event(user, "LOGIN_AD" if login_mode == "ad" else "LOGIN_LOCAL", user.username)
            if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
                return redirect(next_url)
            return redirect("dashboard")
    return render(request, "registration/login.html", {"login_error": error, "next": next_url})


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
