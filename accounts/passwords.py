"""An administrator sets a user's password; the user is signed out everywhere.

The new password is checked with AUTH_PASSWORD_VALIDATORS. Afterwards every
Django session of the user is deleted and every refresh token issued to them
is blacklisted; their access tokens stop working at once because they carry a
hash of the old password hash (SIMPLE_JWT["CHECK_REVOKE_TOKEN"]). The change is
written to the audit log (UserChangeLog, field "password"): who, whose, when,
never the password itself.
"""

from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from .models import User, UserChangeLog

AUDIT_FIELD = "password"
AUDIT_VALUE = "Changed by an administrator"


def may_set_password(actor, target):
    """Any admin may set a worker's password, or their own; another admin's or staff account's needs a superuser."""
    if not (actor.is_active and actor.is_staff):
        return False
    if (target.is_staff or target.is_superuser) and target.pk != actor.pk:
        return actor.is_superuser
    return True


def end_sessions(user_id, now=None):
    """Delete every unexpired Django session that belongs to the user. Returns how many."""
    now = now or timezone.now()
    owned = [session.pk for session in Session.objects.filter(expire_date__gt=now).iterator()
             if session.get_decoded().get("_auth_user_id") == str(user_id)]
    Session.objects.filter(pk__in=owned).delete()
    return len(owned)


def blacklist_refresh_tokens(user_id, now=None):
    """Blacklist every unexpired refresh token issued to the user. Returns how many were newly blacklisted."""
    now = now or timezone.now()
    tokens = OutstandingToken.objects.filter(user_id=user_id, expires_at__gt=now, blacklistedtoken__isnull=True)
    return sum(BlacklistedToken.objects.get_or_create(token=token)[1] for token in tokens)


def set_password_by_admin(*, target, new_password, actor):
    """Validate and set the password, end every session and token, and audit it.

    Raises django.core.exceptions.ValidationError (the validators' messages)
    when the password is not acceptable; nothing is changed then.
    Returns {"sessions_ended": n, "refresh_tokens_blacklisted": m}.
    """
    validate_password(new_password, user=target)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=target.pk)
        user.set_password(new_password)
        user.save(update_fields=["password"])
        result = {"sessions_ended": end_sessions(user.pk), "refresh_tokens_blacklisted": blacklist_refresh_tokens(user.pk)}
        UserChangeLog.objects.create(user=user, field=AUDIT_FIELD, old_value="", new_value=AUDIT_VALUE, changed_by=actor)
    return result


def password_changes(user, limit=5):
    """The latest admin password changes for the user detail view (who and when only)."""
    rows = UserChangeLog.objects.filter(user=user, field=AUDIT_FIELD).select_related("changed_by")[:limit]
    return [{"changed_by": row.changed_by.email if row.changed_by_id else None, "changed_at": row.changed_at.isoformat()} for row in rows]
