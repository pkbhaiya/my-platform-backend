from django.urls import path

from .views import IncomingOrderAPIView, IncomingServicesAPIView
from .admin_wallet_views import (
    AdminWalletAdjustmentAPIView,
    AdminWalletDetailAPIView,
    AdminWalletsAPIView,
    AdminWalletTransactionsAPIView,
    MyWalletAddFundsAPIView,
    MyWalletAPIView,
    MyWalletRegenerateKeyAPIView,
    MyWalletTransactionsAPIView,
)
from .admin_api_views import AdminOrderCreateAPIView, AdminOrderPreviousOrdersAPIView
from common.admin_bulk_delete import AdminBulkDeleteOrdersAPIView

urlpatterns = [
    path("admin/orders/create/", AdminOrderCreateAPIView.as_view(), name="admin-order-create"),
    path("admin/orders/<int:order_id>/previous-orders/", AdminOrderPreviousOrdersAPIView.as_view(), name="admin-order-previous-orders"),
    # My Wallet: the signed-in admin's own wallet.
    path("admin/my-wallet/", MyWalletAPIView.as_view(), name="admin-my-wallet"),
    path("admin/my-wallet/add-funds/", MyWalletAddFundsAPIView.as_view(), name="admin-my-wallet-add-funds"),
    path("admin/my-wallet/regenerate-key/", MyWalletRegenerateKeyAPIView.as_view(), name="admin-my-wallet-regenerate-key"),
    path("admin/my-wallet/transactions/", MyWalletTransactionsAPIView.as_view(), name="admin-my-wallet-transactions"),
    # Admin Wallets: every admin's wallet (superusers only).
    path("admin/admin-wallets/", AdminWalletsAPIView.as_view(), name="admin-wallets"),
    path("admin/admin-wallets/<int:wallet_id>/", AdminWalletDetailAPIView.as_view(), name="admin-wallet"),
    path("admin/admin-wallets/<int:wallet_id>/transactions/", AdminWalletTransactionsAPIView.as_view(), name="admin-wallet-transactions"),
    path("admin/admin-wallets/<int:wallet_id>/adjustments/", AdminWalletAdjustmentAPIView.as_view(), name="admin-wallet-adjustment"),
    path("admin/orders/bulk-delete/", AdminBulkDeleteOrdersAPIView.as_view(), name="admin-orders-bulk-delete"),
    path(
        "orders/incoming/",
        IncomingOrderAPIView.as_view(),
        name="incoming-order",
    ),
    path("services/incoming/", IncomingServicesAPIView.as_view(), name="incoming-services"),
]
