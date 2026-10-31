from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import CanViewOwnRecords

from .models import ReconciliationRun, Wallet, WalletTransaction, WithdrawalRequest
from .reconciliation import unreviewed_reconciliation_alert
from .serializers import (
    AdminWithdrawalRequestSerializer,
    WalletSerializer,
    WalletTransactionSerializer,
    WithdrawalRequestSerializer,
)
from .services import create_withdrawal_request, next_upi_change_at, review_withdrawal_request, set_payout_upi


class UserHistoryPagination(PageNumberPagination):
    page_size = 5
    page_size_query_param = "page_size"
    max_page_size = 5


class MyWalletAPIView(APIView):
    """Return the logged-in user's wallet balance."""

    permission_classes = [IsAuthenticated, CanViewOwnRecords]

    def get(self, request):
        wallet, _ = Wallet.objects.get_or_create(
            user=request.user,
        )

        serializer = WalletSerializer(wallet)

        return Response(serializer.data)


class MyWalletTransactionsAPIView(generics.ListAPIView):
    """Return only the logged-in user's transaction history."""

    serializer_class = WalletTransactionSerializer
    pagination_class = UserHistoryPagination
    permission_classes = [IsAuthenticated, CanViewOwnRecords]

    def get_queryset(self):
        # Reading history never needs to create a wallet; no wallet means no rows.
        transactions = (
            WalletTransaction.objects.filter(wallet__user=self.request.user)
            .order_by("-created_at", "-id")
        )
        transaction_type = self.request.query_params.get("transaction_type", "")
        if transaction_type:
            if transaction_type not in WalletTransaction.TransactionType.values:
                raise ValidationError({"transaction_type": "Invalid transaction type."})
            transactions = transactions.filter(transaction_type=transaction_type)
        return transactions


class MyWithdrawalRequestsAPIView(generics.ListCreateAPIView):
    """Create withdrawals and list only the logged-in user's requests."""

    serializer_class = WithdrawalRequestSerializer
    pagination_class = UserHistoryPagination
    permission_classes = [IsAuthenticated, CanViewOwnRecords]

    def get_queryset(self):
        withdrawals = WithdrawalRequest.objects.filter(user=self.request.user).order_by(
            "-requested_at", "-id"
        )
        withdrawal_status = self.request.query_params.get("status", "")
        if withdrawal_status:
            if withdrawal_status not in WithdrawalRequest.Status.values:
                raise ValidationError({"status": "Invalid withdrawal status."})
            withdrawals = withdrawals.filter(status=withdrawal_status)
        return withdrawals

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            withdrawal = create_withdrawal_request(
                user=request.user,
                amount=serializer.validated_data["amount"],
                payout_method=serializer.validated_data["payout_method"],
            )
        except DjangoValidationError as exc:
            detail = exc.messages if hasattr(exc, "messages") else str(exc)
            raise ValidationError({"detail": detail})

        output = self.get_serializer(withdrawal)
        return Response(output.data, status=status.HTTP_201_CREATED)


def payout_upi_state(user):
    allowed_at = next_upi_change_at(user)
    return {
        "upi_id": user.upi_id,
        "changed_at": user.upi_id_changed_at.isoformat() if user.upi_id_changed_at else None,
        "next_change_allowed_at": allowed_at.isoformat() if allowed_at and allowed_at > timezone.now() else None,
    }


class MyPayoutUpiAPIView(APIView):
    """GET/PUT /api/v1/wallet/payout-upi/: the saved UPI ID that every withdrawal is paid to."""

    permission_classes = [IsAuthenticated, CanViewOwnRecords]

    def get(self, request):
        return Response(payout_upi_state(request.user))

    def put(self, request):
        try:
            user = set_payout_upi(user=request.user, upi_id=request.data.get("upi_id"))
        except DjangoValidationError as exc:
            raise ValidationError({"upi_id": exc.messages})
        return Response(payout_upi_state(user))


class AdminWalletTransactionsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class AdminWalletTransactionsAPIView(APIView):
    """List wallet transactions for administrators."""

    permission_classes = [IsAdminUser]
    pagination_class = AdminWalletTransactionsPagination

    def get(self, request):
        transactions = WalletTransaction.objects.select_related(
            "wallet__user", "task"
        ).order_by("-created_at", "-id")

        user_id = request.query_params.get("user_id")
        if user_id:
            if not user_id.isdigit():
                return Response(
                    {"detail": "user_id must be a positive integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            transactions = transactions.filter(wallet__user_id=int(user_id))

        transaction_type = request.query_params.get("transaction_type")
        valid_types = {
            choice[0] for choice in WalletTransaction.TransactionType.choices
        }

        if transaction_type:
            if transaction_type not in valid_types:
                return Response(
                    {
                        "detail": "Invalid transaction_type.",
                        "valid_types": sorted(valid_types),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            transactions = transactions.filter(transaction_type=transaction_type)

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")

        parsed_from = parse_date(date_from) if date_from else None
        parsed_to = parse_date(date_to) if date_to else None

        if date_from and not parsed_from:
            return Response(
                {"detail": "Invalid date_from. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if date_to and not parsed_to:
            return Response(
                {"detail": "Invalid date_to. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if parsed_from and parsed_to and parsed_from > parsed_to:
            return Response(
                {"detail": "date_from must be before or equal to date_to."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if parsed_from:
            start_datetime = timezone.make_aware(
                datetime.combine(parsed_from, time.min)
            )
            transactions = transactions.filter(created_at__gte=start_datetime)

        if parsed_to:
            end_datetime = timezone.make_aware(
                datetime.combine(
                    parsed_to + timedelta(days=1),
                    time.min,
                )
            )
            transactions = transactions.filter(created_at__lt=end_datetime)

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(
            transactions,
            request,
            view=self,
        )

        results = [
            {
                "id": tx.id,
                "user_id": tx.wallet.user_id,
                "user_email": tx.wallet.user.email,
                "transaction_type": tx.transaction_type,
                "direction": tx.direction,
                "amount": format(tx.amount, ".8f"),
                "balance_after": format(tx.balance_after, ".8f"),
                "task_id": tx.task_id,
                # Set on referral_bonus rows: the Referral that was paid.
                "referral_id": tx.referral_id,
                "description": tx.description,
                "created_at": tx.created_at.isoformat(),
            }
            for tx in page
        ]

        return paginator.get_paginated_response(results)


class AdminWithdrawalRequestsAPIView(generics.ListAPIView):
    """List withdrawal requests for admins, with optional status filtering."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminWithdrawalRequestSerializer

    def get_queryset(self):
        queryset = WithdrawalRequest.objects.select_related(
            "user", "reviewed_by"
        ).order_by("-requested_at", "-id")

        withdrawal_status = self.request.query_params.get("status")

        if withdrawal_status:
            if withdrawal_status not in WithdrawalRequest.Status.values:
                raise ValidationError({"status": "Invalid withdrawal status."})
            queryset = queryset.filter(status=withdrawal_status)

        return queryset


class AdminWithdrawalReviewAPIView(APIView):
    """Allow admins to approve or reject withdrawal requests."""

    permission_classes = [IsAdminUser]

    def post(self, request, withdrawal_id):
        action = request.data.get("action")
        review_note = request.data.get("review_note", "")

        try:
            withdrawal = review_withdrawal_request(
                withdrawal_id=withdrawal_id,
                reviewer=request.user,
                action=action,
                review_note=review_note,
            )
        except DjangoValidationError as exc:
            detail = exc.messages if hasattr(exc, "messages") else str(exc)
            raise ValidationError({"detail": detail})

        return Response(
            {
                "id": withdrawal.id,
                "status": withdrawal.status,
                "amount": str(withdrawal.amount),
                "review_note": withdrawal.review_note,
                "reviewed_at": withdrawal.reviewed_at,
            },
            status=status.HTTP_200_OK,
        )


def reconciliation_row(run):
    return {
        "id": run.pk,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat(),
        "wallets_checked": run.wallets_checked,
        "mismatches_count": run.mismatches_count,
        "details": run.details,
        "reviewed_at": run.reviewed_at.isoformat() if run.reviewed_at else None,
        "reviewed_by": run.reviewed_by.email if run.reviewed_by_id else None,
        "review_note": run.review_note,
    }



class AdminReconciliationRunsAPIView(APIView):
    """GET /api/v1/admin/wallet/reconciliation/: the last 30 runs, newest first (read-only)."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        runs = ReconciliationRun.objects.select_related("reviewed_by").order_by("-started_at", "-id")[:30]
        return Response({"results": [reconciliation_row(run) for run in runs], "alert": unreviewed_reconciliation_alert()})


class AdminReconciliationReviewAPIView(APIView):
    """POST /api/v1/admin/wallet/reconciliation/<id>/review/ {"note": "..."}: mark a run reviewed. Changes no balance."""

    permission_classes = [IsAdminUser]

    def post(self, request, run_id):
        note = request.data.get("note", "")
        note = note.strip() if isinstance(note, str) else ""
        if not note:
            raise ValidationError({"note": "Add a note describing what you checked or did."})
        if len(note) > 2000:
            raise ValidationError({"note": "The note can be at most 2000 characters."})
        updated = ReconciliationRun.objects.filter(pk=run_id, reviewed_at__isnull=True).update(
            reviewed_at=timezone.now(), reviewed_by=request.user, review_note=note,
        )
        run = ReconciliationRun.objects.select_related("reviewed_by").filter(pk=run_id).first()
        if run is None:
            return Response({"detail": "Reconciliation run not found."}, status=status.HTTP_404_NOT_FOUND)
        if not updated:
            raise ValidationError({"detail": "This run has already been reviewed."})
        return Response(reconciliation_row(run))
