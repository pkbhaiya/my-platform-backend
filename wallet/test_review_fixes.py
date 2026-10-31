"""Regression tests for fixes from the October 2026 code review."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase

from common.models import SiteConfiguration
from wallet.models import Wallet, WalletTransaction, WithdrawalRequest
from wallet.services import create_withdrawal_request

User = get_user_model()


def make_user(email, **extra):
    fields = dict(
        email=email, password="TestPassword!9317", full_name="Wallet User",
        platform_username=email.split("@")[0], whatsapp_number="9123400004",
        state="DL", gender="female", approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


class WithdrawalValidationTests(APITestCase):
    def setUp(self):
        configuration = SiteConfiguration.load()
        configuration.min_withdrawal_amount = Decimal("1.00")
        configuration.save()
        self.user = make_user("payout@example.com")
        Wallet.objects.create(user=self.user, balance=Decimal("10.00000000"))
        self.client.force_authenticate(self.user)

    def save_upi(self, upi_id):
        return self.client.put("/api/v1/wallet/payout-upi/", {"upi_id": upi_id}, format="json")

    def request_payout(self):
        return self.client.post("/api/v1/wallet/withdrawals/", {"amount": "5.00", "payout_method": "upi"}, format="json")

    def test_malformed_upi_id_is_rejected_and_balance_untouched(self):
        for bad in ("not-a-upi-id", "name@", "name @bank", "name@bank.com"):
            response = self.save_upi(bad)
            self.assertEqual(response.status_code, 400, bad)
            self.assertEqual(response.data["upi_id"], ["Enter a valid UPI ID, for example name@bank."])
        self.assertEqual(self.request_payout().data["detail"], ["Add your UPI ID before requesting a withdrawal."])
        self.assertFalse(WithdrawalRequest.objects.exists())
        self.assertEqual(Wallet.objects.get(user=self.user).balance, Decimal("10.00000000"))

    def test_valid_upi_id_is_accepted(self):
        self.assertEqual(self.save_upi("Ravi.Kumar-1@OkHdfcBank ").status_code, 200)
        response = self.request_payout()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["payout_details"], "ravi.kumar-1@okhdfcbank")
        self.assertEqual(Wallet.objects.get(user=self.user).balance, Decimal("5.00000000"))

    def test_withdrawal_rechecks_the_locked_account_status(self):
        # Disapproved by a concurrent strike after the request loaded the user.
        User.objects.filter(pk=self.user.pk).update(upi_id="a@bank", approval_status=User.ApprovalStatus.DISAPPROVED)
        with self.assertRaises(ValidationError):
            create_withdrawal_request(user=self.user, amount=Decimal("5"), payout_method="upi")
        self.assertEqual(Wallet.objects.get(user=self.user).balance, Decimal("10.00000000"))
        self.assertFalse(WalletTransaction.objects.exists())


class WalletHistoryTests(APITestCase):
    def test_reading_history_does_not_create_a_wallet(self):
        user = make_user("no-wallet@example.com")
        self.client.force_authenticate(user)
        response = self.client.get("/api/v1/wallet/transactions/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)
        self.assertFalse(Wallet.objects.filter(user=user).exists())
