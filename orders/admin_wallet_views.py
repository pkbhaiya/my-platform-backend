"""Admin API for admin wallets.

My Wallet (every admin, their own wallet only): balance, this month's figures,
add funds, regenerate the API key, ledger. Admin Wallets (superusers only):
every admin's wallet and ledger, read-only except adjustments, which need a
note and are audit-logged. A new key is in its one response only (no-store);
afterwards only its first 8 characters (api_key_prefix) are ever shown.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from common.analytics import period_bounds
from tasks.services import _retry_when_locked

from .admin_api_views import no_store
from .admin_wallets import ensure_admin_wallet, record_add_funds, record_adjustment, regenerate_api_key, update_low_balance_threshold
from .models import AdminWallet, AdminWalletTransaction

Type = AdminWalletTransaction.Type
ZERO = Decimal("0.00")


class IsSuperUser(BasePermission):
    message = "Only a superuser can see other admins' wallets."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.is_staff and user.is_superuser)


class LedgerPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def money(value):
    return None if value is None else format(value, "f")


def when(value):
    return value.isoformat() if value else None


def this_month():
    """(start, end) of the current calendar month in local time (IST)."""
    today = timezone.localdate()
    return period_bounds(today.replace(day=1), today)


def month_figures(wallet_ids):
    """{wallet_id: {added, spent, refunded}} for this calendar month (IST), one query for all wallets."""
    start, end = this_month()
    figures = {wallet_id: {"added": ZERO, "spent": ZERO, "refunded": ZERO} for wallet_id in wallet_ids}
    names = {Type.ADD_FUNDS: "added", Type.ORDER_CHARGE: "spent", Type.REFUND: "refunded"}
    for wallet_id, kind, total in (
        AdminWalletTransaction.objects.filter(wallet_id__in=wallet_ids, created_at__gte=start, created_at__lt=end,
                                              transaction_type__in=list(names))
        .values("wallet_id", "transaction_type").annotate(total=Sum("amount")).values_list("wallet_id", "transaction_type", "total")
    ):
        # SQLite's SUM drops the scale (110 instead of 110.00).
        figures[wallet_id][names[kind]] = Decimal(total).quantize(Decimal("0.01"))
    return figures


def wallet_row(wallet, figures=None):
    month = (figures or month_figures([wallet.pk]))[wallet.pk]
    admin = wallet.admin
    return {
        "id": wallet.pk,
        "admin": {"id": admin.pk, "email": admin.email, "full_name": admin.full_name, "is_superuser": admin.is_superuser,
                  "is_staff": admin.is_staff, "is_active": admin.is_active},
        "balance": money(wallet.balance),
        "low_balance_threshold": money(wallet.low_balance_threshold),
        "low_balance": wallet.balance < wallet.low_balance_threshold,
        "api_key": {"configured": bool(wallet.api_key_hash), "prefix": wallet.api_key_prefix,
                    "created_at": when(wallet.api_key_created_at), "last_used_at": when(wallet.api_key_last_used_at)},
        "this_month": {name: money(value) for name, value in month.items()},
        "created_at": when(wallet.created_at),
    }


def transaction_row(row):
    return {
        "id": row.pk,
        "created_at": when(row.created_at),
        "transaction_type": row.transaction_type,
        "transaction_type_display": row.get_transaction_type_display(),
        "direction": row.direction,
        "amount": money(row.amount),
        "balance_after": money(row.balance_after),
        "order_id": row.order_id,
        "reference": row.reference,
        "note": row.note,
        "created_by": row.created_by.email if row.created_by_id else None,
    }


def refused(exc):
    """A service ValidationError as a DRF 400, keeping per-field messages."""
    if hasattr(exc, "error_dict"):
        return ValidationError(exc.message_dict)
    return ValidationError({"detail": exc.messages})


def loaded(wallet_id):
    return AdminWallet.objects.select_related("admin").get(pk=wallet_id)


def date_param(request, name):
    raw = request.query_params.get(name, "")
    if not raw:
        return None
    try:
        parsed = parse_date(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValidationError({name: f"Invalid {name}. Use YYYY-MM-DD."})
    return parsed


def ledger_response(request, view, wallet):
    """The wallet's ledger, newest first; ?transaction_type=&date_from=&date_to= (local days), 20 per page."""
    rows = AdminWalletTransaction.objects.filter(wallet=wallet).select_related("created_by").order_by("-created_at", "-id")
    kind = request.query_params.get("transaction_type", "")
    if kind:
        if kind not in Type.values:
            raise ValidationError({"transaction_type": f"Choose one of: {', '.join(Type.values)}."})
        rows = rows.filter(transaction_type=kind)
    date_from, date_to = date_param(request, "date_from"), date_param(request, "date_to")
    if date_from and date_to and date_from > date_to:
        raise ValidationError({"date_to": "date_from must be on or before date_to."})
    if date_from:
        rows = rows.filter(created_at__gte=period_bounds(date_from, date_from)[0])
    if date_to:
        rows = rows.filter(created_at__lt=period_bounds(date_to, date_to)[1])
    paginator = LedgerPagination()
    page = paginator.paginate_queryset(rows, request, view=view)
    return paginator.get_paginated_response([transaction_row(row) for row in page])


class FundsSerializer(serializers.Serializer):
    # Strings, so amounts stay exact; the service parses and checks them.
    amount = serializers.CharField(max_length=32, trim_whitespace=True)
    reference = serializers.CharField(required=False, allow_blank=True, default="", max_length=200)
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=2000)
    request_key = serializers.UUIDField(required=False, allow_null=True, default=None)


# ---------------------------------------------------------------------------
# My Wallet: the signed-in admin's own wallet
# ---------------------------------------------------------------------------

class MyWalletAPIView(APIView):
    """GET the signed-in admin's wallet; PATCH {low_balance_threshold}."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        wallet = ensure_admin_wallet(request.user)
        return Response({**wallet_row(loaded(wallet.pk)), "can_view_all_wallets": request.user.is_superuser})

    def patch(self, request):
        wallet = ensure_admin_wallet(request.user)
        data = request.data if isinstance(request.data, dict) else {}
        unknown = set(data) - {"low_balance_threshold"}
        if unknown or "low_balance_threshold" not in data:
            raise ValidationError({"detail": ["Only low_balance_threshold can be changed here."]})
        try:
            _retry_when_locked(update_low_balance_threshold, wallet_id=wallet.pk, low_balance_threshold=data["low_balance_threshold"])
        except DjangoValidationError as exc:
            raise refused(exc)
        return Response({**wallet_row(loaded(wallet.pk)), "can_view_all_wallets": request.user.is_superuser})


class MyWalletAddFundsAPIView(APIView):
    """POST {amount, reference (UPI UTR), note?, request_key?}; a repeated request_key returns the first result."""

    permission_classes = [IsAdminUser]

    def post(self, request):
        wallet = ensure_admin_wallet(request.user)
        serializer = FundsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            row, created = _retry_when_locked(record_add_funds, wallet_id=wallet.pk, amount=data["amount"], reference=data["reference"],
                                              note=data["note"], by=request.user, request_key=data["request_key"])
        except DjangoValidationError as exc:
            raise refused(exc)
        row = AdminWalletTransaction.objects.select_related("created_by").get(pk=row.pk)
        return Response({"transaction": transaction_row(row), "wallet": wallet_row(loaded(wallet.pk)), "created": created},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class MyWalletRegenerateKeyAPIView(APIView):
    """POST: a new personal key, in this response only. The old key stops working immediately."""

    permission_classes = [IsAdminUser]

    def post(self, request):
        wallet = ensure_admin_wallet(request.user)
        try:
            wallet, key = _retry_when_locked(regenerate_api_key, wallet_id=wallet.pk)
        except DjangoValidationError as exc:
            raise refused(exc)
        return no_store(Response({"wallet": wallet_row(loaded(wallet.pk)), "api_key": key}, status=status.HTTP_201_CREATED))


class MyWalletTransactionsAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        return ledger_response(request, self, ensure_admin_wallet(request.user))


# ---------------------------------------------------------------------------
# Admin Wallets: every admin's wallet (superusers only)
# ---------------------------------------------------------------------------

class AdminWalletsAPIView(APIView):
    """GET every staff user's wallet with balance and this month's figures."""

    permission_classes = [IsSuperUser]

    def get(self, request):
        for admin in get_user_model().objects.filter(is_staff=True, admin_wallet__isnull=True):
            ensure_admin_wallet(admin)
        wallets = list(AdminWallet.objects.select_related("admin").order_by("admin__email", "id"))
        figures = month_figures([wallet.pk for wallet in wallets])
        return Response({"results": [wallet_row(wallet, figures) for wallet in wallets]})


class AdminWalletDetailAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request, wallet_id):
        return Response(wallet_row(get_object_or_404(AdminWallet.objects.select_related("admin"), pk=wallet_id)))


class AdminWalletTransactionsAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request, wallet_id):
        return ledger_response(request, self, get_object_or_404(AdminWallet, pk=wallet_id))


class AdminWalletAdjustmentAPIView(APIView):
    """POST {amount (+ adds, - takes away), note (required), request_key?}. Audit-logged on the wallet owner."""

    permission_classes = [IsSuperUser]

    def post(self, request, wallet_id):
        get_object_or_404(AdminWallet, pk=wallet_id)
        serializer = FundsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            row, created = _retry_when_locked(record_adjustment, wallet_id=wallet_id, amount=data["amount"], note=data["note"],
                                              by=request.user, request_key=data["request_key"])
        except DjangoValidationError as exc:
            raise refused(exc)
        row = AdminWalletTransaction.objects.select_related("created_by").get(pk=row.pk)
        return Response({"transaction": transaction_row(row), "wallet": wallet_row(loaded(wallet_id)), "created": created},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
