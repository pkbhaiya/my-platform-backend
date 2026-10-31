from django.urls import path

from .views import (
    MyWalletAPIView,
    MyWalletTransactionsAPIView,
    AdminWalletTransactionsAPIView,
    MyWithdrawalRequestsAPIView,
    MyPayoutUpiAPIView,
    AdminWithdrawalReviewAPIView,
    AdminWithdrawalRequestsAPIView,
    AdminReconciliationReviewAPIView,
    AdminReconciliationRunsAPIView,
)

urlpatterns = [
    path(
        "wallet/",
        MyWalletAPIView.as_view(),
        name="my-wallet",
    ),
    path(
        "wallet/transactions/",
        MyWalletTransactionsAPIView.as_view(),
        name="my-wallet-transactions",
    ),
    path(
        "admin/wallet/transactions/",
        AdminWalletTransactionsAPIView.as_view(),
        name="admin-wallet-transactions",
    ),
    path("wallet/payout-upi/", MyPayoutUpiAPIView.as_view(), name="my-payout-upi"),
    path("admin/wallet/reconciliation/", AdminReconciliationRunsAPIView.as_view(), name="admin-reconciliation"),
    path("admin/wallet/reconciliation/<int:run_id>/review/", AdminReconciliationReviewAPIView.as_view(), name="admin-reconciliation-review"),
    path(
        "wallet/withdrawals/",
        MyWithdrawalRequestsAPIView.as_view(),
        name="my-withdrawals",
    ),
    path(
        "admin/wallet/withdrawals/<int:withdrawal_id>/review/",
        AdminWithdrawalReviewAPIView.as_view(),
        name="admin-withdrawal-review",
    ),
    path(
        "admin/wallet/withdrawals/",
        AdminWithdrawalRequestsAPIView.as_view(),
        name="admin-withdrawals",
    ),
]
