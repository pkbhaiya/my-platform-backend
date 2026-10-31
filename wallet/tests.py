from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase
from common.models import SiteConfiguration
from django.core.exceptions import ValidationError
from wallet.models import Wallet, WalletTransaction, WithdrawalRequest
from wallet.services import (
    create_withdrawal_request,
    review_withdrawal_request,
)


class WalletAPITests(APITestCase):

    def create_approved_user(self, email):
        User = get_user_model()

        return User.objects.create(
            email=email,
            full_name="Test User",
            platform_username=email.split("@")[0],
            whatsapp_number="9876543210",
            state="DL",
            gender="female",
            approval_status="approved",
            is_active=True,
        )

    def setUp(self):
        self.user = self.create_approved_user("walletuser@example.com")
        self.other_user = self.create_approved_user("otheruser@example.com")

        self.wallet = Wallet.objects.create(
            user=self.user,
            balance=Decimal("10.00000000"),
        )

        self.other_wallet = Wallet.objects.create(
            user=self.other_user,
            balance=Decimal("50.00000000"),
        )

        WalletTransaction.objects.create(
            wallet=self.wallet,
            transaction_type=WalletTransaction.TransactionType.ADJUSTMENT,
            direction=WalletTransaction.Direction.CREDIT,
            amount=Decimal("10.00000000"),
            balance_after=Decimal("10.00000000"),
            description="Test credit",
        )

        WalletTransaction.objects.create(
            wallet=self.other_wallet,
            transaction_type=WalletTransaction.TransactionType.ADJUSTMENT,
            direction=WalletTransaction.Direction.CREDIT,
            amount=Decimal("50.00000000"),
            balance_after=Decimal("50.00000000"),
            description="Other user's credit",
        )

        self.client.force_authenticate(user=self.user)

    def test_user_can_view_own_wallet(self):
        response = self.client.get(reverse("my-wallet"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Decimal(response.data["balance"]),
            Decimal("10.00000000"),
        )

    def test_user_only_sees_own_transactions(self):
        response = self.client.get(reverse("my-wallet-transactions"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(
            response.data["results"][0]["description"],
            "Test credit",
        )

    def test_user_history_apis_cap_page_size_at_five(self):
        for index in range(6):
            WalletTransaction.objects.create(
                wallet=self.wallet,
                transaction_type=WalletTransaction.TransactionType.ADJUSTMENT,
                direction=WalletTransaction.Direction.CREDIT,
                amount=Decimal("1.00000000"),
                balance_after=Decimal("10.00000000"),
                description=f"Extra {index}",
            )
            WithdrawalRequest.objects.create(
                user=self.user, amount=Decimal("1.00000000"),
                payout_method=WithdrawalRequest.PayoutMethod.UPI,
                payout_details="test@example",
            )
        transactions = self.client.get(reverse("my-wallet-transactions"), {"page_size": 100})
        withdrawals = self.client.get(reverse("my-withdrawals"), {"page_size": 100})
        self.assertEqual(transactions.status_code, 200)
        self.assertEqual(transactions.data["count"], 7)
        self.assertEqual(len(transactions.data["results"]), 5)
        self.assertIsNotNone(transactions.data["next"])
        self.assertEqual(withdrawals.status_code, 200)
        self.assertEqual(withdrawals.data["count"], 6)
        self.assertEqual(len(withdrawals.data["results"]), 5)
        self.assertIsNotNone(withdrawals.data["next"])

    def test_wallet_endpoint_requires_login(self):
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse("my-wallet"))

        self.assertEqual(response.status_code, 401)


class AdminWalletTransactionsAPITests(APITestCase):
    def setUp(self):
        User = get_user_model()

        self.admin = User.objects.create(
            email="admin-ledger@example.com",
            full_name="Test Admin",
            platform_username="admin_ledger",
            whatsapp_number="9000000001",
            state="DL",
            gender="female",
            approval_status="approved",
            is_active=True,
            is_staff=True,
        )

        self.user = User.objects.create(
            email="ledger-user@example.com",
            full_name="Ledger User",
            platform_username="ledger_user",
            whatsapp_number="9000000002",
            state="DL",
            gender="female",
            approval_status="approved",
            is_active=True,
        )

        self.other_user = User.objects.create(
            email="other-ledger@example.com",
            full_name="Other User",
            platform_username="other_ledger",
            whatsapp_number="9000000003",
            state="DL",
            gender="female",
            approval_status="approved",
            is_active=True,
        )

        self.wallet = Wallet.objects.create(
            user=self.user,
            balance=Decimal("8.00000000"),
        )

        self.other_wallet = Wallet.objects.create(
            user=self.other_user,
            balance=Decimal("50.00000000"),
        )

        WalletTransaction.objects.create(
            wallet=self.wallet,
            transaction_type=WalletTransaction.TransactionType.ADJUSTMENT,
            direction=WalletTransaction.Direction.CREDIT,
            amount=Decimal("10.00000000"),
            balance_after=Decimal("10.00000000"),
            description="Test credit",
        )

        WalletTransaction.objects.create(
            wallet=self.wallet,
            transaction_type=WalletTransaction.TransactionType.PAYOUT,
            direction=WalletTransaction.Direction.DEBIT,
            amount=Decimal("2.00000000"),
            balance_after=Decimal("8.00000000"),
            description="Test payout",
        )

        WalletTransaction.objects.create(
            wallet=self.other_wallet,
            transaction_type=WalletTransaction.TransactionType.ADJUSTMENT,
            direction=WalletTransaction.Direction.CREDIT,
            amount=Decimal("50.00000000"),
            balance_after=Decimal("50.00000000"),
            description="Other user credit",
        )

        self.url = "/api/v1/admin/wallet/transactions/"

    def test_admin_can_list_wallet_transactions(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(len(response.data["results"]), 3)
        self.assertIn("user_email", response.data["results"][0])

    def test_admin_can_filter_by_user_and_transaction_type(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            self.url,
            {
                "user_id": self.user.id,
                "transaction_type": "adjustment",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["user_id"],
            self.user.id,
        )
        self.assertEqual(
            response.data["results"][0]["transaction_type"],
            "adjustment",
        )

    def test_admin_wallet_transactions_reject_invalid_type(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            self.url,
            {"transaction_type": "invalid_type"},
        )

        self.assertEqual(response.status_code, 400)

    def test_regular_user_cannot_list_all_wallet_transactions(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 403)

    def test_admin_wallet_transactions_pagination(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            self.url,
            {"page_size": 1},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertIn("next", response.data)
        self.assertIn("previous", response.data)

    def test_admin_can_filter_transactions_by_date(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            self.url,
            {
                "date_from": "2020-01-01",
                "date_to": "2099-12-31",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)

    def test_admin_transactions_reject_invalid_date(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            self.url,
            {"date_from": "invalid-date"},
        )

        self.assertEqual(response.status_code, 400)


class WithdrawalServiceTests(APITestCase):

    def setUp(self):
        User = get_user_model()

        SiteConfiguration.objects.create(min_withdrawal_amount=Decimal("25.00"))

        self.user = User.objects.create(
            email="withdrawaluser@example.com",
            full_name="Withdrawal User",
            platform_username="withdrawaluser",
            whatsapp_number="9876543210",
            state="DL",
            gender="female",
            approval_status="approved",
            is_active=True,
        )

        self.admin = User.objects.create(
            email="withdrawaladmin@example.com",
            full_name="Withdrawal Admin",
            platform_username="withdrawaladmin",
            whatsapp_number="9876543211",
            state="DL",
            gender="female",
            is_active=True,
            is_staff=True,
        )

        self.wallet = Wallet.objects.create(
            user=self.user,
            balance=Decimal("100.00000000"),
        )
        # Withdrawals are paid to the saved UPI ID.
        type(self.user).objects.filter(pk=self.user.pk).update(upi_id="user@example")

    def create_withdrawal(self):
        return create_withdrawal_request(
            user=self.user,
            amount=Decimal("25.00000000"),
            payout_method="upi",
        )

    def test_request_deducts_wallet_balance(self):
        withdrawal = self.create_withdrawal()

        self.wallet.refresh_from_db()
        self.assertEqual(withdrawal.status, WithdrawalRequest.Status.PENDING)
        self.assertEqual(self.wallet.balance, Decimal("75.00000000"))

    def test_insufficient_balance_is_rejected(self):
        with self.assertRaises(ValidationError):
            create_withdrawal_request(
                user=self.user,
                amount=Decimal("150.00000000"),
                payout_method="upi",
            )

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("100.00000000"))
        self.assertEqual(WithdrawalRequest.objects.count(), 0)

    def test_approval_does_not_deduct_twice(self):
        withdrawal = self.create_withdrawal()

        reviewed = review_withdrawal_request(
            withdrawal_id=withdrawal.id,
            reviewer=self.admin,
            action="approve",
            review_note="Approved",
        )

        self.wallet.refresh_from_db()
        self.assertEqual(reviewed.status, WithdrawalRequest.Status.APPROVED)
        self.assertEqual(self.wallet.balance, Decimal("75.00000000"))

    def test_rejection_refunds_wallet(self):
        withdrawal = self.create_withdrawal()

        reviewed = review_withdrawal_request(
            withdrawal_id=withdrawal.id,
            reviewer=self.admin,
            action="reject",
            review_note="Details could not be verified",
        )

        self.wallet.refresh_from_db()
        self.assertEqual(reviewed.status, WithdrawalRequest.Status.REJECTED)
        self.assertEqual(self.wallet.balance, Decimal("100.00000000"))

        refund = WalletTransaction.objects.filter(
            description=f"Refund for rejected withdrawal #{withdrawal.id}"
        ).first()

        self.assertIsNotNone(refund)
        self.assertEqual(refund.direction, WalletTransaction.Direction.CREDIT)

    def test_withdrawal_cannot_be_reviewed_twice(self):
        withdrawal = self.create_withdrawal()

        review_withdrawal_request(
            withdrawal_id=withdrawal.id,
            reviewer=self.admin,
            action="reject",
            review_note="Rejected",
        )

        with self.assertRaises(ValidationError):
            review_withdrawal_request(
                withdrawal_id=withdrawal.id,
                reviewer=self.admin,
                action="approve",
                review_note="Second review",
            )

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("100.00000000"))

    def test_withdrawal_below_minimum_is_rejected(self):
        with self.assertRaises(ValidationError):
            create_withdrawal_request(
                user=self.user,
                amount=Decimal("24.99"),
                payout_method="upi",
            )

        self.wallet.refresh_from_db()
        self.assertEqual(
            self.wallet.balance,
            Decimal("100.00000000"),
        )
        self.assertEqual(WithdrawalRequest.objects.count(), 0)


class WithdrawalAPITests(APITestCase):
    def setUp(self):
        SiteConfiguration.objects.update_or_create(
            pk=1,
            defaults={"min_withdrawal_amount": Decimal("25.00")},
        )

        User = get_user_model()

        self.user = User.objects.create(
            email="withdrawalapi@example.com",
            full_name="Withdrawal API User",
            platform_username="withdrawalapi",
            whatsapp_number="9876543210",
            state="DL",
            gender="female",
            approval_status="approved",
            is_active=True,
        )

        self.admin = User.objects.create(
            email="withdrawalapiadmin@example.com",
            full_name="Withdrawal API Admin",
            platform_username="withdrawalapiadmin",
            whatsapp_number="9876543211",
            state="DL",
            gender="female",
            is_active=True,
            is_staff=True,
        )

        self.wallet = Wallet.objects.create(
            user=self.user,
            balance=Decimal("100.00000000"),
        )
        # Withdrawals are paid to the saved UPI ID.
        type(self.user).objects.filter(pk=self.user.pk).update(upi_id="user@example")

    def test_user_can_create_withdrawal_through_api(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            reverse("my-withdrawals"),
            {
                "amount": "25.00",
                "payout_method": "upi",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)

        self.wallet.refresh_from_db()
        self.assertEqual(
            self.wallet.balance,
            Decimal("75.00000000"),
        )

    def test_api_rejects_amount_below_minimum(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            reverse("my-withdrawals"),
            {
                "amount": "24.99",
                "payout_method": "upi",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)

        self.wallet.refresh_from_db()
        self.assertEqual(
            self.wallet.balance,
            Decimal("100.00000000"),
        )

    def test_regular_user_cannot_access_admin_withdrawals(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.get(reverse("admin-withdrawals"))

        self.assertEqual(response.status_code, 403)

    def test_admin_can_reject_withdrawal_through_api(self):
        withdrawal = create_withdrawal_request(
            user=self.user,
            amount=Decimal("25.00"),
            payout_method="upi",
        )

        self.client.force_authenticate(user=self.admin)

        response = self.client.post(
            reverse(
                "admin-withdrawal-review",
                kwargs={"withdrawal_id": withdrawal.id},
            ),
            {
                "action": "reject",
                "review_note": "Test rejection",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)

        self.wallet.refresh_from_db()
        self.assertEqual(
            self.wallet.balance,
            Decimal("100.00000000"),
        )
