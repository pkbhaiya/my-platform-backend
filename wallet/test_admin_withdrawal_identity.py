from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from common.models import SiteConfiguration
from wallet.models import Wallet, WithdrawalRequest
from wallet.serializers import AdminWithdrawalRequestSerializer
from wallet.services import create_withdrawal_request

User = get_user_model()
ADMIN_URL = "/api/v1/admin/wallet/withdrawals/"


def make_user(index, **extra):
    fields = dict(
        email=f"payout-user-{index}@example.com", password="TestPassword!9317",
        full_name=f"Payout User {index}", platform_username=f"payout_handle_{index}",
        whatsapp_number=f"93456{index:05d}", state="DL", gender="female",
        approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


class AdminWithdrawalIdentityTests(APITestCase):
    def setUp(self):
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"min_withdrawal_amount": Decimal("10.00")})
        self.admin = make_user(0, is_staff=True)
        self.first = make_user(1)
        self.second = make_user(2)
        for user, upi_id in ((self.first, "first@okbank"), (self.second, "second@okbank")):
            Wallet.objects.create(user=user, balance=Decimal("500.00000000"))
            type(user).objects.filter(pk=user.pk).update(upi_id=upi_id)
        self.first_request = create_withdrawal_request(user=self.first, amount=Decimal("125.50"), payout_method="upi")
        self.second_request = create_withdrawal_request(user=self.second, amount=Decimal("40.00"), payout_method="upi")

    def test_admin_response_identifies_the_real_requester_of_each_withdrawal(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(ADMIN_URL)
        self.assertEqual(response.status_code, 200)
        rows = {row["id"]: row for row in response.data}
        first = rows[self.first_request.pk]
        self.assertEqual(first["user_id"], self.first.pk)
        self.assertEqual(first["user_full_name"], "Payout User 1")
        self.assertEqual(first["user_platform_username"], "payout_handle_1")
        self.assertEqual(first["user_email"], "payout-user-1@example.com")
        self.assertEqual((first["amount"], first["currency"]), ("125.50000000", "INR"))
        self.assertEqual(first["upi_id"], "first@okbank")
        self.assertEqual(first["status"], "pending")
        self.assertTrue(first["requester_available"])
        # Dates are serialized in the configured Asia/Kolkata zone.
        self.assertTrue(first["requested_at"].endswith("+05:30"), first["requested_at"])
        # Each request is tied to its own user, never another requester's.
        self.assertEqual(rows[self.second_request.pk]["user_id"], self.second.pk)
        self.assertEqual(rows[self.second_request.pk]["upi_id"], "second@okbank")

    def test_status_filter_keeps_identity_and_review_outcome(self):
        self.client.force_authenticate(user=self.admin)
        review = self.client.post(f"{ADMIN_URL}{self.second_request.pk}/review/", {"action": "reject"}, format="json")
        self.assertEqual(review.status_code, 200)
        rejected = self.client.get(f"{ADMIN_URL}?status=rejected").data
        self.assertEqual([row["id"] for row in rejected], [self.second_request.pk])
        self.assertEqual(rejected[0]["user_email"], "payout-user-2@example.com")
        self.assertEqual(rejected[0]["reviewed_by_email"], self.admin.email)

    def test_missing_user_reference_produces_an_explicit_fallback_not_a_guess(self):
        legacy = WithdrawalRequest(amount=Decimal("5.00"), payout_method="upi", payout_details="legacy@upi", status="pending")
        data = AdminWithdrawalRequestSerializer(legacy).data
        self.assertFalse(data["requester_available"])
        for field in ("user_id", "user_full_name", "user_platform_username", "user_email"):
            self.assertIsNone(data[field], field)
        self.assertEqual(data["upi_id"], "legacy@upi")

    def test_non_admins_cannot_read_requester_identity(self):
        self.client.force_authenticate(user=self.first)
        self.assertEqual(self.client.get(ADMIN_URL).status_code, 403)
        own = self.client.get("/api/v1/wallet/withdrawals/")
        self.assertEqual(own.status_code, 200)
        self.assertEqual([row["id"] for row in own.data["results"]], [self.first_request.pk])
        for field in ("user_id", "user_email", "user_full_name", "user_platform_username"):
            self.assertNotIn(field, own.data["results"][0])
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(ADMIN_URL).status_code, 401)
