"""Phase 10-FIX Step 1: every account gets one 8-character referral code, wherever it was created."""

import os
import re
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.admin import CustomUserAdmin
from accounts.models import User
from accounts.referral_codes import generate_referral_code
from accounts.test_registration_control import payload as signup_payload
from common.models import SiteConfiguration
from tasks.test_claim_lifecycle import make_user

CODE = re.compile(r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$")


class ReferralCodeCreationTests(TestCase):
    def setUp(self):
        # Sign-up is rate-limited per IP; start each test with a fresh limit.
        cache.clear()
        self.addCleanup(cache.clear)

    def assert_code(self, user):
        user.refresh_from_db()
        self.assertRegex(user.referral_code, CODE)

    def test_sign_up_api(self):
        response = APIClient().post("/api/v1/auth/signup/", signup_payload(1), format="json")
        self.assertEqual(response.status_code, 201)
        self.assert_code(User.objects.get(email="closed-1@example.com"))

    def test_create_user_and_create_superuser(self):
        self.assert_code(make_user(1))
        self.assert_code(User.objects.create_superuser(
            email="root@example.com", password="Root-Password-8812", full_name="Root", platform_username="root",
            whatsapp_number="9000000001", state="DL", gender="female"))

    def test_createsuperuser_command(self):
        with patch.dict(os.environ, {"DJANGO_SUPERUSER_PASSWORD": "Root-Password-8812"}):
            call_command("createsuperuser", interactive=False, email="cli@example.com", full_name="Cli Admin",
                         platform_username="cli_admin", whatsapp_number="9000000002", state="DL", gender="female",
                         stdout=StringIO())
        self.assert_code(User.objects.get(email="cli@example.com"))

    def test_django_admin_add_form(self):
        admin = User.objects.create_superuser(
            email="root@example.com", password="Root-Password-8812", full_name="Root", platform_username="root",
            whatsapp_number="9000000001", state="DL", gender="female")
        self.client.force_login(admin)
        response = self.client.post(reverse("admin:accounts_user_add"), {
            "email": "added@example.com", "full_name": "Added", "platform_username": "added_user",
            "whatsapp_number": "9000000003", "state": "DL", "gender": "female",
            "password1": "Added-Password-8812", "password2": "Added-Password-8812", "usable_password": "true",
        })
        self.assertEqual(response.status_code, 302, response.content[:500])
        self.assert_code(User.objects.get(email="added@example.com"))

    def test_codes_are_unique(self):
        codes = [make_user(index).referral_code for index in range(1, 41)]
        self.assertEqual(len(set(codes)), len(codes))

    def test_a_colliding_code_is_drawn_again(self):
        taken = make_user(1).referral_code
        # The race: the pre-check saw the code as free, but another account saved it first.
        with patch("accounts.models.generate_referral_code", side_effect=[taken, "NEWCQDE2"]):
            user = make_user(2)
        self.assertEqual(user.referral_code, "NEWCQDE2")
        self.assertEqual(User.objects.filter(referral_code=taken).count(), 1)

    def test_other_integrity_errors_are_not_retried(self):
        make_user(1)
        with patch("accounts.models.generate_referral_code", wraps=generate_referral_code) as generate:
            with self.assertRaises(IntegrityError):
                User.objects.create_user(email="life-1@example.com", password="x", full_name="Dup", platform_username="dup_user",
                                         whatsapp_number="9000000009", state="DL", gender="female")
        self.assertEqual(generate.call_count, 1)

    def test_the_code_is_read_only(self):
        user = make_user(1)
        code = user.referral_code
        self.assertFalse(User._meta.get_field("referral_code").editable)
        self.assertIn("referral_code", CustomUserAdmin.readonly_fields)
        # Someone else's code at sign-up is the inviter's code, never the new account's own.
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"referral_enabled": True})
        response = APIClient().post("/api/v1/auth/signup/", signup_payload(2, invite_code=code), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(User.objects.get(email="closed-2@example.com").referral_code, code)
        # Saving other fields keeps the code.
        user.full_name = "Renamed"
        user.save()
        user.refresh_from_db()
        self.assertEqual(user.referral_code, code)
