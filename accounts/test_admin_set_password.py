"""Phase 11 (revised), part B: an admin sets any user's password; the user is signed out everywhere."""

from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import Client, TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import UserChangeLog
from tasks.test_claim_lifecycle import make_user

NEW = "Fresh-Orchid-4821"
OLD = "TestPassword!9317"  # make_user's password
OK_MESSAGE = "Password updated. The user has been logged out of all devices."


def url(user):
    return f"/api/v1/admin/users/{user.pk}/set-password/"


class SetPasswordTestCase(TestCase):
    def setUp(self):
        cache.clear()  # the login throttle counts per IP in the shared cache
        self.addCleanup(cache.clear)
        self.admin = make_user(0, is_staff=True)
        self.worker = make_user(1)
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def set_password(self, user, password=NEW, confirm=None, client=None):
        return (client or self.api).post(url(user), {"new_password": password, "confirm_password": password if confirm is None else confirm},
                                         format="json")

    def login(self, user, password):
        return APIClient().post("/api/v1/auth/login/", {"email": user.email, "password": password}, format="json")


class SetPasswordTests(SetPasswordTestCase):
    def test_an_admin_sets_a_workers_password_without_knowing_the_old_one(self):
        response = self.set_password(self.worker)
        self.assertEqual((response.status_code, response.data["detail"]), (200, OK_MESSAGE))
        self.assertEqual(self.login(self.worker, NEW).status_code, 200)
        self.assertEqual(self.login(self.worker, OLD).status_code, 401)
        self.assertNotIn(NEW, response.content.decode())

    def test_django_password_validators_return_field_errors(self):
        mismatch = self.set_password(self.worker, NEW, confirm=NEW + "x")
        self.assertEqual((mismatch.status_code, list(mismatch.data)), (400, ["confirm_password"]))
        weak = self.set_password(self.worker, "1234567")
        self.assertEqual((weak.status_code, list(weak.data)), (400, ["new_password"]))
        messages = " ".join(weak.data["new_password"])
        for expected in ("too short", "too common", "entirely numeric"):
            self.assertIn(expected, messages)
        similar = self.set_password(self.worker, self.worker.email.split("@")[0] + "1")
        self.assertIn("too similar", " ".join(similar.data["new_password"]))
        self.assertEqual(self.api.post(url(self.worker), {"new_password": NEW}, format="json").status_code, 400)
        self.assertEqual(self.login(self.worker, OLD).status_code, 200)  # nothing changed

    def test_only_a_superuser_may_change_another_admins_password(self):
        other_admin = make_user(2, is_staff=True)
        superuser = make_user(3, is_staff=True, is_superuser=True)
        for target in (other_admin, superuser):
            response = self.set_password(target)
            self.assertEqual(response.status_code, 403, target)
            self.assertIn("superuser", response.data["detail"])
        self.assertEqual(self.set_password(self.admin).status_code, 200)  # their own password
        root = APIClient()
        root.force_authenticate(superuser)
        self.assertEqual(self.set_password(other_admin, client=root).status_code, 200)
        self.assertEqual(self.login(other_admin, NEW).status_code, 200)

    def test_workers_and_anonymous_callers_are_refused(self):
        worker_api = APIClient()
        worker_api.force_authenticate(make_user(4))
        self.assertEqual(self.set_password(self.worker, client=worker_api).status_code, 403)
        self.assertIn(self.set_password(self.worker, client=APIClient()).status_code, (401, 403))
        self.assertEqual(self.api.post("/api/v1/admin/users/999999/set-password/", {"new_password": NEW, "confirm_password": NEW},
                                       format="json").status_code, 404)

    def test_the_change_is_audit_logged_without_the_password(self):
        self.set_password(self.worker)
        row = UserChangeLog.objects.get(user=self.worker, field="password")
        self.assertEqual((row.changed_by_id, row.old_value, row.new_value), (self.admin.pk, "", "Changed by an administrator"))
        self.assertFalse(UserChangeLog.objects.filter(new_value__contains=NEW).exists())
        detail = self.api.get(f"/api/v1/admin/users/{self.worker.pk}/").data["password_changes"]
        self.assertEqual(detail[0]["changed_by"], self.admin.email)


class SignedOutEverywhereTests(SetPasswordTestCase):
    def test_every_session_is_ended(self):
        browser = Client()
        browser.force_login(self.worker)
        other_user = Client()
        other_user.force_login(make_user(5))
        sessions = lambda user: [s for s in Session.objects.all() if s.get_decoded().get("_auth_user_id") == str(user.pk)]  # noqa: E731
        self.assertEqual(len(sessions(self.worker)), 1)
        response = self.set_password(self.worker)
        self.assertEqual(response.data["sessions_ended"], 1)
        self.assertEqual(sessions(self.worker), [])
        self.assertEqual(Session.objects.count(), 1)  # someone else's session is untouched
        self.assertIn(browser.get("/api/v1/auth/me/").status_code, (401, 403))

    def test_refresh_tokens_are_blacklisted_and_access_tokens_stop_at_once(self):
        tokens = [self.login(self.worker, OLD).data for _ in range(2)]  # two devices
        device = APIClient()
        device.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens[0]['access']}")
        self.assertEqual(device.get("/api/v1/auth/me/").status_code, 200)
        response = self.set_password(self.worker)
        self.assertEqual(response.data["refresh_tokens_blacklisted"], 2)
        self.assertEqual(BlacklistedToken.objects.filter(token__user=self.worker).count(), 2)
        self.assertEqual(device.get("/api/v1/auth/me/").status_code, 401)
        for pair in tokens:
            self.assertEqual(APIClient().post("/api/v1/auth/refresh/", {"refresh": pair["refresh"]}, format="json").status_code, 401)
        fresh = self.login(self.worker, NEW).data
        again = APIClient()
        again.credentials(HTTP_AUTHORIZATION=f"Bearer {fresh['access']}")
        self.assertEqual(again.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(APIClient().post("/api/v1/auth/refresh/", {"refresh": fresh["refresh"]}, format="json").status_code, 200)

    def test_a_refresh_token_without_the_password_claim_is_refused(self):
        # Issued before CHECK_REVOKE_TOKEN (and possibly before the blacklist): it could never be revoked otherwise.
        token = RefreshToken.for_user(self.worker)
        del token["hash_password"]
        response = APIClient().post("/api/v1/auth/refresh/", {"refresh": str(token)}, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(OutstandingToken.objects.filter(user=self.worker).count(), 1)  # recorded at issue
