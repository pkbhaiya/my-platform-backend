"""Referral rules: created at signup, paid once when the referred user qualifies.

- A referral exists only if it was created at signup; it is never added later.
- The referred user qualifies at ``required_tasks_snapshot`` APPROVED claims
  (rejected, expired, cancelled and Target Not Available never count), before
  ``qualify_deadline``, while their account is Approved.
- The referrer gets one bonus (``bonus_amount_snapshot``) through the wallet
  ledger. The referral row lock plus a conditional status update and the unique
  ledger link mean concurrent approvals cannot pay twice.
- Referrer and referred sharing a UPI ID, device or IP address flags the
  referral; flagged referrals are never paid automatically.
- The monthly cap (per referrer, IST calendar month) leaves extra referrals
  Qualified; the daily ``process_referrals`` command pays them later.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from accounts.models import AccountSignal, User
from accounts.referral_codes import normalize_referral_code
from common.models import SiteConfiguration
from common.notifications import rupees, send_admin_alert
from tasks.models import TaskClaim
from wallet.models import PayoutUpiChange, WalletTransaction
from wallet.services import credit_referral_bonus

from .models import Referral

SETTING_FIELDS = (
    "referral_enabled", "referral_required_approved_tasks", "referral_bonus_amount",
    "referral_qualify_within_days", "referral_monthly_bonus_cap",
)
INACTIVE_REFERRER = "Referrer account is not active."


def referral_settings():
    defaults = {name: SiteConfiguration._meta.get_field(name).default for name in SETTING_FIELDS}
    return SiteConfiguration.objects.filter(pk=1).values(*SETTING_FIELDS).first() or defaults


def mask_username(value):
    """"ravi1992" -> "ra***92": enough to recognise a friend, never the full handle."""
    name = (value or "").strip().lstrip("@")
    if len(name) <= 4:
        return f"{name[:1]}***" if name else "***"
    return f"{name[:2]}***{name[-2:]}"


def eligible_referrer(code):
    """The account a referral code belongs to, if it may refer people: active, approved, not staff.

    Codes of pending, disapproved or disabled accounts are ignored at sign-up
    (the new account is created without a referral).
    """
    code = normalize_referral_code(code)
    if not code:
        return None
    return User.objects.filter(
        referral_code=code, is_active=True, is_staff=False, is_superuser=False, approval_status=User.ApprovalStatus.APPROVED,
    ).first()


def approved_claims(user_id):
    return TaskClaim.objects.filter(user_id=user_id, status=TaskClaim.Status.APPROVED).count()


def ist_month_start(now):
    local = timezone.localtime(now)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def shared_signal_reasons(referrer_id, referred_id):
    """Why two accounts look like the same person: shared UPI ID, device or IP address."""
    reasons = []
    upis = {}
    for user_id in (referrer_id, referred_id):
        known = {value for pair in PayoutUpiChange.objects.filter(user_id=user_id).values_list("previous_upi_id", "new_upi_id") for value in pair if value}
        current = User.objects.filter(pk=user_id).values_list("upi_id", flat=True).first()
        upis[user_id] = known | ({current} if current else set())
    if upis[referrer_id] & upis[referred_id]:
        reasons.append("Shares a UPI ID with the referrer.")
    signals = AccountSignal.objects.filter(user_id__in=(referrer_id, referred_id)).values_list("user_id", "signal_type", "value_hash")
    hashes = {referrer_id: {}, referred_id: {}}
    for user_id, signal_type, value_hash in signals:
        kind = "device" if signal_type == AccountSignal.SignalType.DEVICE_ID else "IP address"
        hashes[user_id].setdefault(kind, set()).add(value_hash)
    for kind in ("device", "IP address"):
        if hashes[referrer_id].get(kind, set()) & hashes[referred_id].get(kind, set()):
            reasons.append(f"Shares a{'n' if kind[0] in 'aeiouAEIOU' else ''} {kind} with the referrer.")
    return reasons


def _flag(referral, reason, now, from_statuses):
    changed = Referral.objects.filter(pk=referral.pk, status__in=from_statuses).update(
        status=Referral.Status.FLAGGED, flag_reason=reason[:255],
    )
    if changed:
        referral.status, referral.flag_reason = Referral.Status.FLAGGED, reason[:255]
        send_admin_alert("referral_flagged", (
            f"Referral #{referral.pk} (user #{referral.referrer_id} referred user #{referral.referred_id}) "
            f"was flagged for review: {reason}"
        ))
    return bool(changed)


def flag_if_abusive(referral, now):
    """Flag a pending or qualified referral whose accounts share a UPI ID, device or IP address."""
    if referral.abuse_cleared:
        return False
    reasons = shared_signal_reasons(referral.referrer_id, referral.referred_id)
    return bool(reasons) and _flag(referral, " ".join(reasons), now, (Referral.Status.PENDING, Referral.Status.QUALIFIED))


INVITE_CODE_INVALID = "This invite code is not valid. Check it or leave the field empty."
INVITE_CODE_INACTIVE = "This invite code is not active. Leave the field empty to continue."
INVITE_CODE_REQUIRED = "Registration is currently invite-only. Enter a valid invite code."
INVITE_CODE_APPLIED = "Invite code applied."


def normalize_invite_code(value):
    """The invite code as typed, without spaces and in capitals ("" when empty)."""
    return "".join(value.split()).upper() if isinstance(value, str) else ""


def check_invite_code(value):
    """(referrer, error message) for a sign-up invite code; (None, None) when it is empty.

    An invite code is a user's referral code. It works only while referrals are
    on and its owner may refer people (eligible_referrer); otherwise the person
    signing up is told, instead of the code being dropped silently.
    """
    code = normalize_invite_code(value)
    if not code:
        return None, None
    if not normalize_referral_code(code) or not User.objects.filter(referral_code=code).exists():
        return None, INVITE_CODE_INVALID
    referrer = eligible_referrer(code) if referral_settings()["referral_enabled"] else None
    return (referrer, None) if referrer else (None, INVITE_CODE_INACTIVE)


def create_signup_referral(*, referred, code, now=None):
    """Create the referral for a new account, or None when the code is not usable. Never raises for a bad code."""
    settings_ = referral_settings()
    if not settings_["referral_enabled"]:
        return None
    referrer = eligible_referrer(code)
    if referrer is None or referrer.pk == referred.pk:
        return None
    now = now or timezone.now()
    days = settings_["referral_qualify_within_days"]
    referral = Referral.objects.create(
        referrer=referrer,
        referred=referred,
        required_tasks_snapshot=settings_["referral_required_approved_tasks"],
        bonus_amount_snapshot=settings_["referral_bonus_amount"],
        qualify_deadline=now + timedelta(days=days) if days else None,
    )
    flag_if_abusive(referral, now)
    return referral


def _paid_this_month(referrer_id, now):
    return Referral.objects.filter(referrer_id=referrer_id, status=Referral.Status.PAID, paid_at__gte=ist_month_start(now)).count()


def _pay(referral, now, *, override=False):
    """Pay a locked, qualified (or admin-approved) referral. Returns True when it was paid.

    Without ``override`` an inactive referrer flags the referral and the
    monthly cap leaves it Qualified for a later month.
    """
    referrer = User.objects.select_for_update().get(pk=referral.referrer_id)
    if not override:
        if not referrer.is_active or referrer.approval_status in (User.ApprovalStatus.DISAPPROVED, User.ApprovalStatus.DISABLED):
            _flag(referral, INACTIVE_REFERRER, now, (Referral.Status.QUALIFIED,))
            return False
        cap = referral_settings()["referral_monthly_bonus_cap"]
        if cap and _paid_this_month(referral.referrer_id, now) >= cap:
            return False
    referred_name = User.objects.filter(pk=referral.referred_id).values_list("platform_username", flat=True).first()
    credit_referral_bonus(referral=referral, description=f"Referral bonus — user {mask_username(referred_name)}")
    Referral.objects.filter(pk=referral.pk).update(status=Referral.Status.PAID, paid_at=now)
    referral.status, referral.paid_at = Referral.Status.PAID, now
    return True


def _evaluate_pending(referral, now):
    """A locked pending referral: expire it, flag it, qualify it and try to pay. Returns the new status."""
    if referral.qualify_deadline and now > referral.qualify_deadline:
        Referral.objects.filter(pk=referral.pk, status=Referral.Status.PENDING).update(status=Referral.Status.EXPIRED)
        return Referral.Status.EXPIRED
    if approved_claims(referral.referred_id) < referral.required_tasks_snapshot:
        return referral.status
    referred = User.objects.filter(pk=referral.referred_id).values("is_active", "approval_status").first()
    if not referred or not referred["is_active"] or referred["approval_status"] != User.ApprovalStatus.APPROVED:
        return referral.status
    if flag_if_abusive(referral, now):
        return Referral.Status.FLAGGED
    Referral.objects.filter(pk=referral.pk, status=Referral.Status.PENDING).update(status=Referral.Status.QUALIFIED, qualified_at=now)
    referral.status, referral.qualified_at = Referral.Status.QUALIFIED, now
    _pay(referral, now)
    return referral.status


def on_claim_approved(user_id, now=None):
    """Called inside the claim-approval transaction, after the claim is approved and rewarded."""
    referral = Referral.objects.select_for_update().filter(referred_id=user_id, status=Referral.Status.PENDING).first()
    if referral is not None:
        _evaluate_pending(referral, now or timezone.now())


# ---------------------------------------------------------------------------
# Daily job and admin decisions
# ---------------------------------------------------------------------------

def process_referrals(now=None):
    """Expire overdue pending referrals, re-check them for abuse, qualify late ones and pay capped ones."""
    now = now or timezone.now()
    result = {"expired": 0, "flagged": 0, "paid": 0}
    for referral_id in Referral.objects.filter(status=Referral.Status.PENDING).order_by("id").values_list("pk", flat=True):
        with transaction.atomic():
            referral = Referral.objects.select_for_update().filter(pk=referral_id, status=Referral.Status.PENDING).first()
            if referral is None:
                continue
            if referral.qualify_deadline is None or now <= referral.qualify_deadline:
                if flag_if_abusive(referral, now):
                    result["flagged"] += 1
                    continue
            status = _evaluate_pending(referral, now)
            result["expired"] += status == Referral.Status.EXPIRED
            result["paid"] += status == Referral.Status.PAID
    for referral_id in Referral.objects.filter(status=Referral.Status.QUALIFIED).order_by("qualified_at", "id").values_list("pk", flat=True):
        with transaction.atomic():
            referral = Referral.objects.select_for_update().filter(pk=referral_id, status=Referral.Status.QUALIFIED).first()
            if referral is None:
                continue
            if flag_if_abusive(referral, now):
                result["flagged"] += 1
            elif _pay(referral, now):
                result["paid"] += 1
    paid = Referral.objects.filter(status=Referral.Status.PAID, paid_at__gte=now - timedelta(days=1), paid_at__lte=now)
    totals = paid.aggregate(count=Count("id"), amount=Sum("bonus_amount_snapshot"))
    if totals["count"]:
        send_admin_alert("referral_summary", (
            f"Referral bonuses paid in the last 24 hours: {totals['count']}, total {rupees(totals['amount'] or Decimal('0'))}."
        ))
    return result


class ReferralDecisionError(Exception):
    pass


@transaction.atomic
def approve_flagged_referral(*, referral_id, admin, note="", now=None):
    """Admin approval of a flagged referral. Idempotent.

    Requirement met: paid now (the admin's decision overrides the cap and the
    referrer's status). Not met yet: back to Pending with the flag cleared, so it
    pays automatically when it qualifies; past the deadline: Expired.
    """
    now = now or timezone.now()
    referral = Referral.objects.select_for_update().filter(pk=referral_id).first()
    if referral is None:
        raise ReferralDecisionError("Referral not found.")
    if referral.status == Referral.Status.PAID:
        return referral
    if referral.status != Referral.Status.FLAGGED:
        raise ReferralDecisionError("Only a flagged referral can be approved.")
    Referral.objects.filter(pk=referral.pk).update(abuse_cleared=True, reviewed_by=admin, reviewed_at=now, review_note=(note or "")[:2000])
    referral.abuse_cleared = True
    if approved_claims(referral.referred_id) >= referral.required_tasks_snapshot:
        Referral.objects.filter(pk=referral.pk).update(status=Referral.Status.QUALIFIED, qualified_at=referral.qualified_at or now)
        referral.status = Referral.Status.QUALIFIED
        _pay(referral, now, override=True)
    elif referral.qualify_deadline and now > referral.qualify_deadline:
        Referral.objects.filter(pk=referral.pk).update(status=Referral.Status.EXPIRED)
    else:
        Referral.objects.filter(pk=referral.pk).update(status=Referral.Status.PENDING)
    return Referral.objects.get(pk=referral.pk)


@transaction.atomic
def reject_referral(*, referral_id, admin, note, now=None):
    """Admin rejection of a flagged referral, with a note. Idempotent; a paid referral cannot be rejected."""
    note = (note or "").strip() if isinstance(note, str) else ""
    if not note:
        raise ReferralDecisionError("Add a note explaining the rejection.")
    referral = Referral.objects.select_for_update().filter(pk=referral_id).first()
    if referral is None:
        raise ReferralDecisionError("Referral not found.")
    if referral.status == Referral.Status.REJECTED:
        return referral
    if referral.status != Referral.Status.FLAGGED:
        raise ReferralDecisionError("Only a flagged referral can be rejected.")
    Referral.objects.filter(pk=referral.pk).update(
        status=Referral.Status.REJECTED, reviewed_by=admin, reviewed_at=now or timezone.now(), review_note=note[:2000],
    )
    return Referral.objects.get(pk=referral.pk)


def referral_link_path(code):
    return f"/signup?ref={code}"


def referrer_summary(user):
    """Counts and earnings for the Refer & Earn page."""
    counts = Referral.objects.filter(referrer=user).aggregate(
        invited=Count("id"),
        in_progress=Count("id", filter=Q(status__in=(Referral.Status.PENDING, Referral.Status.QUALIFIED, Referral.Status.FLAGGED))),
        paid=Count("id", filter=Q(status=Referral.Status.PAID)),
    )
    earned = sum(WalletTransaction.objects.filter(
        referral__referrer=user, transaction_type=WalletTransaction.TransactionType.REFERRAL_BONUS,
    ).values_list("amount", flat=True), Decimal("0"))
    return {**counts, "total_earned": format(earned.quantize(Decimal("0.01")), "f")}
