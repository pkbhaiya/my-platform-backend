"""State: one coded list; chosen at sign-up and never changed by the user (admins may correct it)."""

import importlib
import os
import re
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.constants import INDIAN_STATES, INDIAN_UNION_TERRITORIES, STATE_CHOICES, state_code_for
from accounts.models import User, UserChangeLog
from accounts.test_registration_control import payload as signup_payload
from tasks.test_claim_lifecycle import make_user

SIGNUP = "/api/v1/auth/signup/"
ME = "/api/v1/auth/me/"
INVALID = ["Please select a valid state."]
LOCKED = ["State cannot be changed after sign-up."]

# The frontend renders the list from this file without a network call. Set
# FRONTEND_DIR when the frontend is not checked out next to this project.
FRONTEND_STATES = Path(os.environ.get("FRONTEND_DIR") or Path(settings.BASE_DIR).parent / "my-platform-frontend") / "src/constants/states.js"
JS_ENTRY = re.compile(r"\[\s*'([A-Z]{2})',\s*'([^']+)'\s*\]")


class StateListTests(TestCase):
    def test_36_states_and_union_territories_in_one_alphabetical_list(self):
        self.assertEqual((len(INDIAN_STATES), len(INDIAN_UNION_TERRITORIES), len(STATE_CHOICES)), (28, 8, 36))
        names = [name for _code, name in STATE_CHOICES]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len({code for code, _name in STATE_CHOICES}), 36)

    def test_the_frontend_list_is_identical(self):
        if not FRONTEND_STATES.exists():
            self.skipTest(f"Frontend not found at {FRONTEND_STATES}; set FRONTEND_DIR to compare the state lists.")
        frontend = JS_ENTRY.findall(FRONTEND_STATES.read_text(encoding="utf-8"))
        self.assertEqual(frontend, list(STATE_CHOICES), "src/constants/states.js differs from accounts/constants.py")


class SignupStateTests(TestCase):
    def setUp(self):
        # Sign-up is rate-limited per IP; start each test with a fresh limit.
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_valid_code_is_stored_and_shown_by_name(self):
        response = APIClient().post(SIGNUP, signup_payload(1, state="TN"), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(email="closed-1@example.com")
        self.assertEqual((user.state, user.get_state_display()), ("TN", "Tamil Nadu"))

    def test_state_is_required_and_must_be_on_the_list(self):
        for index, state in enumerate(["Delhi", "XX", "", None, "dl"], start=1):
            with self.subTest(state=state):
                response = APIClient().post(SIGNUP, signup_payload(index, state=state), format="json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["state"], INVALID)
        missing = signup_payload(9)
        del missing["state"]
        self.assertEqual(APIClient().post(SIGNUP, missing, format="json").data["state"], INVALID)
        self.assertFalse(User.objects.filter(email__startswith="closed-").exists())


class ProfileStateTests(TestCase):
    def api_for(self, user):
        api = APIClient()
        api.force_authenticate(user)
        return api

    def test_users_cannot_change_the_state_chosen_at_sign_up(self):
        user = make_user(1, state="DL")
        api = self.api_for(user)
        response = api.patch(ME, {"state": "KA"}, format="json")
        self.assertEqual((response.status_code, response.data["state"]), (400, LOCKED))
        # Sending the same value changes nothing and is not an error.
        self.assertEqual(api.patch(ME, {"state": "DL"}, format="json").status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.state, "DL")
        self.assertFalse(UserChangeLog.objects.exists())

    def test_an_older_account_without_a_state_sets_it_once(self):
        user = make_user(1, approval_status=User.ApprovalStatus.PENDING, state=None)
        api = self.api_for(user)
        self.assertIsNone(api.get(ME).data["state_display"])
        self.assertEqual(api.patch(ME, {"state": "Mumbai"}, format="json").data["state"], INVALID)
        response = api.patch(ME, {"state": "MH"}, format="json")
        self.assertEqual((response.status_code, response.data["state"], response.data["state_display"]), (200, "MH", "Maharashtra"))
        log = UserChangeLog.objects.get()
        self.assertEqual((log.user, log.field, log.old_value, log.new_value, log.changed_by), (user, "state", "", "MH", user))
        # Now it is locked like everyone else's.
        self.assertEqual(api.patch(ME, {"state": "GA"}, format="json").data["state"], LOCKED)
        # Only the state is accepted here.
        api.patch(ME, {"full_name": "Changed", "approval_status": "approved"}, format="json")
        user.refresh_from_db()
        self.assertEqual((user.state, user.full_name, user.approval_status), ("MH", "Life 1", "pending"))


class AdminStateTests(TestCase):
    def setUp(self):
        self.admin = make_user(1, is_staff=True)
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def test_admin_list_shows_and_filters_by_state(self):
        karnataka, unset = make_user(2, state="KA"), make_user(3, state=None)
        rows = {row["id"]: row for row in self.api.get("/api/v1/auth/users/?page_size=100").data["results"]}
        self.assertNotIn("state", rows[karnataka.pk])  # the list shows only its columns; the state is in the detail view
        detail = self.api.get(f"/api/v1/admin/users/{karnataka.pk}/").data
        self.assertEqual((detail["state"], detail["state_display"]), ("KA", "Karnataka"))
        self.assertIsNone(self.api.get(f"/api/v1/admin/users/{unset.pk}/").data["state_display"])
        self.assertEqual([row["id"] for row in self.api.get("/api/v1/auth/users/?state=KA").data["results"]], [karnataka.pk])
        self.assertEqual([row["id"] for row in self.api.get("/api/v1/auth/users/?state=none").data["results"]], [unset.pk])
        self.assertEqual(self.api.get("/api/v1/auth/users/?state=Karnataka").status_code, 400)
        self.assertEqual(self.api.get(f"/api/v1/admin/users/{karnataka.pk}/").data["state_display"], "Karnataka")

    def test_admin_can_correct_a_state_and_the_change_is_logged(self):
        user = make_user(2, state="DL")
        url = f"/api/v1/admin/users/{user.pk}/profile/"
        response = self.api.patch(url, {"state": "UP"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["state"], response.data["state_display"]), ("UP", "Uttar Pradesh"))
        change = response.data["profile_changes"][0]
        self.assertEqual((change["field"], change["old"], change["new_display"], change["changed_by"], change["by_user"]), ("state", "DL", "Uttar Pradesh", self.admin.email, False))
        log = UserChangeLog.objects.get()
        self.assertEqual((log.user, log.old_value, log.new_value, log.changed_by), (user, "DL", "UP", self.admin))
        self.assertEqual(self.api.get(f"/api/v1/admin/users/{user.pk}/").data["profile_changes"][0]["new"], "UP")
        # Saving the same value records nothing; an invalid one is refused.
        self.api.patch(url, {"state": "UP"}, format="json")
        self.assertEqual(UserChangeLog.objects.count(), 1)
        self.assertEqual(self.api.patch(url, {"state": "Lucknow"}, format="json").data["state"], INVALID)

    def test_only_admins_can_correct_a_state(self):
        user = make_user(2, state="DL")
        worker = APIClient()
        worker.force_authenticate(user)
        self.assertEqual(worker.patch(f"/api/v1/admin/users/{user.pk}/profile/", {"state": "UP"}, format="json").status_code, 403)
        user.refresh_from_db()
        self.assertEqual(user.state, "DL")


class StateMigrationTests(TestCase):
    def test_free_text_matching_a_name_becomes_its_code_and_the_rest_is_cleared(self):
        migration = importlib.import_module("accounts.migrations.0013_state_codes")
        rows = [(1, "Delhi"), (2, " delhi "), (3, "tamil   NADU"), (4, "DL"), (5, "Bombay"), (6, ""), (7, None), (8, "Jammu & Kashmir")]
        changes, unmapped = migration.mapped_states(rows)
        self.assertEqual(changes, {1: "DL", 2: "DL", 3: "TN", 4: "DL", 5: None, 6: None, 7: None, 8: None})
        self.assertEqual(unmapped, [(5, "Bombay"), (8, "Jammu & Kashmir")])
        self.assertEqual(state_code_for("Dadra and Nagar Haveli and Daman and Diu"), "DH")
