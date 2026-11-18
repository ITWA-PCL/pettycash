import json
from collections import Counter

from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import User


class Command(BaseCommand):
    help = "Read-only audit of local account password policy readiness and legacy accounts without password_changed_at."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Output the audit report as formatted JSON.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        active_session_counts: Counter[str] = Counter()
        for session in Session.objects.filter(expire_date__gt=now):
            try:
                user_id = session.get_decoded().get("_auth_user_id")
            except Exception:
                continue
            if user_id is not None:
                active_session_counts[str(user_id)] += 1

        users = list(User.objects.all().order_by("username"))
        ad_users = [u for u in users if not u.is_local_account]
        local_users = [u for u in users if u.is_local_account]

        missing_changed_at = []
        must_change = []
        expired_90_days = []
        unusable_local = []
        affected_accounts = []

        for u in local_users:
            reasons = []
            if u.password_changed_at is None:
                missing_changed_at.append(u.username)
                reasons.append("missing_password_changed_at")
            if u.must_change_password:
                must_change.append(u.username)
                reasons.append("must_change_password")
            is_expired, days_left = u.check_password_expired()
            if u.password_changed_at is not None and is_expired and not u.must_change_password:
                expired_90_days.append(u.username)
                reasons.append("expired_90_days")
            elif u.password_changed_at is not None and (now - u.password_changed_at).days >= 90:
                if u.username not in expired_90_days:
                    expired_90_days.append(u.username)
                if "expired_90_days" not in reasons:
                    reasons.append("expired_90_days")
            if not u.has_usable_password():
                unusable_local.append(u.username)
                reasons.append("unusable_password")

            if is_expired:
                affected_accounts.append(
                    {
                        "username": u.username,
                        "is_active": u.is_active,
                        "is_superuser": u.is_superuser,
                        "date_joined": u.date_joined.isoformat() if u.date_joined else None,
                        "password_changed_at": u.password_changed_at.isoformat() if u.password_changed_at else None,
                        "must_change_password": u.must_change_password,
                        "has_usable_password": u.has_usable_password(),
                        "days_left": days_left,
                        "reasons": reasons,
                        "active_session_count": active_session_counts.get(str(u.pk), 0),
                    }
                )

        affected_active = [item for item in affected_accounts if item["is_active"]]
        report = {
            "generated_at": now.isoformat(),
            "summary": {
                "total_users": len(users),
                "active_users": sum(1 for u in users if u.is_active),
                "ad_users": len(ad_users),
                "active_ad_users": sum(1 for u in ad_users if u.is_active),
                "local_users": len(local_users),
                "active_local_users": sum(1 for u in local_users if u.is_active),
                "local_missing_password_changed_at": len(missing_changed_at),
                "local_must_change_password": len(must_change),
                "local_expired_90_days": len(expired_90_days),
                "local_unusable_password": len(unusable_local),
                "total_affected_local_accounts": len(affected_accounts),
                "active_affected_local_accounts": len(affected_active),
                "affected_active_sessions": sum(item["active_session_count"] for item in affected_active),
            },
            "affected_accounts": affected_accounts,
        }

        if options.get("as_json"):
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            return

        s = report["summary"]
        self.stdout.write("=== Local Password Policy Audit (Read-Only) ===")
        self.stdout.write(
            f"Users: total={s['total_users']} (active={s['active_users']}), "
            f"AD={s['ad_users']} (active={s['active_ad_users']}), "
            f"Local={s['local_users']} (active={s['active_local_users']})"
        )
        self.stdout.write(
            f"Affected Local Accounts: total={s['total_affected_local_accounts']}, "
            f"active={s['active_affected_local_accounts']}, "
            f"active_sessions={s['affected_active_sessions']}"
        )
        self.stdout.write(
            f"Breakdown: missing_password_changed_at={s['local_missing_password_changed_at']}, "
            f"must_change_password={s['local_must_change_password']}, "
            f"expired_90_days={s['local_expired_90_days']}, "
            f"unusable_password={s['local_unusable_password']}"
        )
        if affected_accounts:
            self.stdout.write("Accounts requiring password change:")
            for item in affected_accounts:
                self.stdout.write(
                    f"  - {item['username']} (active={item['is_active']}, superuser={item['is_superuser']}, "
                    f"sessions={item['active_session_count']}, reasons={','.join(item['reasons'])})"
                )
        else:
            self.stdout.write("No local accounts currently require forced password change.")
