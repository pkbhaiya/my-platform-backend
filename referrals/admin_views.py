from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from wallet.models import WalletTransaction

from .models import Referral
from .services import (
    ReferralDecisionError, approve_flagged_referral, ist_month_start, referral_link_path, referrer_summary, reject_referral,
)
from .views import approved_count_subquery

CENTS = Decimal("0.01")


def admin_row(referral):
    credit = getattr(referral, "bonus_entry", None)
    return {
        "id": referral.pk,
        "referrer": {"id": referral.referrer_id, "username": referral.referrer.platform_username},
        "referred": {
            "id": referral.referred_id, "username": referral.referred.platform_username,
            "joined_at": referral.referred.date_joined.isoformat(),
        },
        "approved_tasks": referral.approved,
        "required_tasks": referral.required_tasks_snapshot,
        "status": referral.status,
        "status_display": referral.get_status_display(),
        "bonus_amount": format(referral.bonus_amount_snapshot, "f"),
        "flag_reason": referral.flag_reason,
        "created_at": referral.created_at.isoformat(),
        "qualify_deadline": referral.qualify_deadline.isoformat() if referral.qualify_deadline else None,
        "qualified_at": referral.qualified_at.isoformat() if referral.qualified_at else None,
        "paid_at": referral.paid_at.isoformat() if referral.paid_at else None,
        # The wallet credit itself (WalletTransaction type referral_bonus), when paid.
        "credit": {
            "transaction_id": credit.pk,
            "amount": format(credit.amount.quantize(CENTS), "f"),
            "credited_at": credit.created_at.isoformat(),
        } if credit else None,
        "reviewed_by": referral.reviewed_by.email if referral.reviewed_by_id else None,
        "reviewed_at": referral.reviewed_at.isoformat() if referral.reviewed_at else None,
        "review_note": referral.review_note,
    }


def annotated(queryset):
    return (queryset.select_related("referrer", "referred", "reviewed_by", "bonus_entry")
            .annotate(approved=approved_count_subquery()))


def summary(referrals):
    """Counts and bonus total for a filtered set of referrals."""
    counts = referrals.aggregate(
        total=Count("id"),
        qualified=Count("id", filter=Q(status__in=(Referral.Status.QUALIFIED, Referral.Status.PAID))),
        paid=Count("id", filter=Q(status=Referral.Status.PAID)),
        flagged=Count("id", filter=Q(status=Referral.Status.FLAGGED)),
    )
    # Exact: Python sums the Decimals (SQLite would return a float SUM).
    paid = sum(WalletTransaction.objects.filter(referral__in=referrals.values("pk")).values_list("amount", flat=True), Decimal("0"))
    return {**counts, "bonus_paid": format(paid.quantize(CENTS), "f")}


def user_referrals(user, limit=100):
    """The Referrals section of the admin user detail."""
    from accounts.admin_views import referred_by

    made = annotated(Referral.objects.filter(referrer=user).order_by("-created_at", "-id"))
    return {
        "code": user.referral_code,
        "link_path": referral_link_path(user.referral_code),
        "referred_by": referred_by(user),
        "referred_users": [admin_row(referral) for referral in made[:limit]],
        "referred_count": made.count(),
        "total_earned": referrer_summary(user)["total_earned"],
    }


class AdminReferralPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class AdminReferralsAPIView(APIView):
    """GET /api/v1/admin/referrals/?status=&referrer=<id or username>&referred=<id>&id=&date_from=&date_to= (signup date, IST).

    ``summary`` covers every referral matching the filters, not just the page.
    """

    permission_classes = [IsAdminUser]

    def get(self, request):
        referrals = Referral.objects.order_by("-created_at", "-id")
        for name, field in (("id", "pk"), ("referred", "referred_id")):
            raw = request.query_params.get(name, "").strip()
            if raw:
                if not raw.isdigit():
                    raise ValidationError({name: "Use a whole number."})
                referrals = referrals.filter(**{field: int(raw)})
        status_value = request.query_params.get("status", "")
        if status_value:
            if status_value not in Referral.Status.values:
                raise ValidationError({"status": "Invalid referral status."})
            referrals = referrals.filter(status=status_value)
        referrer = request.query_params.get("referrer", "").strip()
        if referrer:
            match = Q(referrer__platform_username__iexact=referrer.lstrip("@"))
            if referrer.isdigit():
                match |= Q(referrer_id=int(referrer))
            referrals = referrals.filter(match)
        for name, lookup, shift in (("date_from", "created_at__gte", 0), ("date_to", "created_at__lt", 1)):
            raw = request.query_params.get(name, "")
            if raw:
                day = parse_date(raw)
                if not day:
                    raise ValidationError({name: "Use YYYY-MM-DD."})
                referrals = referrals.filter(**{lookup: timezone.make_aware(datetime.combine(day + timedelta(days=shift), time.min))})
        paginator = AdminReferralPagination()
        page = paginator.paginate_queryset(annotated(referrals), request, view=self)
        response = paginator.get_paginated_response([admin_row(referral) for referral in page])
        response.data["summary"] = summary(referrals)
        return response


class AdminReferralDecisionAPIView(APIView):
    """POST /api/v1/admin/referrals/<id>/approve/ or /reject/ {"note": "..."} on a flagged referral."""

    permission_classes = [IsAdminUser]
    action = None

    def post(self, request, referral_id):
        note = request.data.get("note", "")
        try:
            if self.action == "approve":
                approve_flagged_referral(referral_id=referral_id, admin=request.user, note=note if isinstance(note, str) else "")
            else:
                reject_referral(referral_id=referral_id, admin=request.user, note=note)
        except ReferralDecisionError as exc:
            if str(exc) == "Referral not found.":
                get_object_or_404(Referral, pk=referral_id)
            raise ValidationError({"detail": str(exc)})
        return Response(admin_row(annotated(Referral.objects.filter(pk=referral_id)).get()))


class AdminTopReferrersAPIView(APIView):
    """GET /api/v1/admin/referrals/top/: referrers with the most bonuses paid this calendar month (IST)."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        since = ist_month_start(timezone.now())
        rows = (Referral.objects.filter(status=Referral.Status.PAID, paid_at__gte=since)
                .values("referrer_id", "referrer__platform_username")
                .annotate(paid=Count("id"), amount=Sum("bonus_amount_snapshot"))
                .order_by("-paid", "-amount", "referrer_id")[:10])
        return Response({
            "month_start": since.date().isoformat(),
            "results": [{
                "referrer": {"id": row["referrer_id"], "username": row["referrer__platform_username"]},
                "paid": row["paid"],
                # Bonuses have 2 decimal places; SQLite returns the SUM as a float.
                "amount": format(Decimal(row["amount"]).quantize(Decimal("0.01")), "f"),
            } for row in rows],
        })
