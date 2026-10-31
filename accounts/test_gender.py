"""Gender: Male or Female at sign-up; never changed by the user; older values kept until an admin updates them."""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, UserChangeLog
from accounts.test_registration_control import payload as signup_payload
from tasks.test_claim_lifecycle import make_user

SIGNUP = "/api/v1/auth/signup/"
ME = "/api/v1/auth/me/"
INVALID = ["Please select Male or Female."]
LOCKED = ["Gender cannot be changed after sign-up."]


class SignupGenderTests(TestCase):
    def setUp(self):
        # Sign-up is rate-limited per IP; start each test with a fresh limit.
        cache.clear()
        self.addCleanup(cache.clear)

    def test_male_and_female_are_accepted(self):
        for index, gender in enumerate(["male", "female"], start=1):
            response = APIClient().post(SIGNUP, signup_payload(index, gender=gender), format="json")
            self.assertEqual(response.status_code, 201, response.data)
            user = User.objects.get(email=f"closed-{index}@example.com")
            self.assertEqual((user.gender, user.gender_label), (gender, gender.capitalize()))

    def test_other_values_and_empty_are_refused(self):
        for index, gender in enumerate(["other", "not_disclosed", "Male", "", None, "x"], start=1):
            with self.subTest(gender=gender):
                response = APIClient().post(SIGNUP, signup_payload(index, gender=gender), format="json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["gender"], INVALID)
        missing = signup_payload(9)
        del missing["gender"]
        self.assertEqual(APIClient().post(SIGNUP, missing, format="json").data["gender"], INVALID)
        self.assertFalse(User.objects.filter(email__startswith="closed-").exists())


class UserCannotChangeGenderTests(TestCase):
    def test_a_change_from_my_profile_is_refused(self):
        user = make_user(1, gender="male")
        api = APIClient()
        api.force_authenticate(user)
        self.assertEqual(api.get(ME).data["gender_display"], "Male")
        response = api.patch(ME, {"gender": "female"}, format="json")
        self.assertEqual((response.status_code, response.data["gender"]), (400, LOCKED))
        # The same value is not a change.
        self.assertEqual(api.patch(ME, {"gender": "male"}, format="json").status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.gender, "male")

    def test_an_older_value_stays_readable_and_locked_for_the_user(self):
        user = make_user(1, gender="other")
        api = APIClient()
        api.force_authenticate(user)
        data = api.get(ME).data
        self.assertEqual((data["gender"], data["gender_display"]), ("other", "Other"))
        self.assertEqual(api.patch(ME, {"gender": "female"}, format="json").data["gender"], LOCKED)
        user.refresh_from_db()
        self.assertEqual(user.gender, "other")
        self.assertTrue(user.gender_needs_update)


class AdminGenderTests(TestCase):
    def setUp(self):
        self.admin = make_user(9, is_staff=True)
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def test_admin_updates_an_older_value_and_the_change_is_logged(self):
        user = make_user(1, gender="not_disclosed")
        detail = self.api.get(f"/api/v1/admin/users/{user.pk}/").data
        self.assertEqual((detail["gender"], detail["gender_display"], detail["gender_needs_update"]), ("not_disclosed", "Prefer not to say", True))
        response = self.api.patch(f"/api/v1/admin/users/{user.pk}/profile/", {"gender": "female"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["gender"], response.data["gender_display"], response.data["gender_needs_update"]), ("female", "Female", False))
        change = response.data["profile_changes"][0]
        self.assertEqual((change["field"], change["old_display"], change["new_display"], change["changed_by"]), ("gender", "Prefer not to say", "Female", self.admin.email))
        log = UserChangeLog.objects.get()
        self.assertEqual((log.field, log.old_value, log.new_value, log.changed_by), ("gender", "not_disclosed", "female", self.admin))

    def test_admin_can_correct_state_and_gender_together_and_only_to_valid_values(self):
        user = make_user(1, gender="male", state="DL")
        url = f"/api/v1/admin/users/{user.pk}/profile/"
        self.assertEqual(self.api.patch(url, {"gender": "other"}, format="json").data["gender"], INVALID)
        self.assertEqual(self.api.patch(url, {}, format="json").status_code, 400)
        self.api.patch(url, {"gender": "female", "state": "KA"}, format="json")
        user.refresh_from_db()
        self.assertEqual((user.gender, user.state), ("female", "KA"))
        self.assertEqual(sorted(UserChangeLog.objects.values_list("field", flat=True)), ["gender", "state"])

    def test_only_admins(self):
        user = make_user(1, gender="male")
        worker = APIClient()
        worker.force_authenticate(user)
        self.assertEqual(worker.patch(f"/api/v1/admin/users/{user.pk}/profile/", {"gender": "female"}, format="json").status_code, 403)

    def test_the_migration_leaves_existing_rows_untouched(self):
        # Choices are Django-level only: older values are still stored and read back as they were.
        user = make_user(1, gender="other")
        User.objects.filter(pk=user.pk).update(gender="not_disclosed")
        self.assertEqual(User.objects.get(pk=user.pk).gender, "not_disclosed")
