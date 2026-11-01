"""The frontend calls the API from its own origin with cookies included."""

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.test_registration_control import SIGNUP, payload
from tasks.test_claim_lifecycle import make_order_task, make_user

FRONTEND = "https://pythonsmm.com"


@override_settings(CORS_ALLOWED_ORIGINS=[FRONTEND], CORS_ALLOWED_ORIGIN_REGEXES=[])
class FrontendOriginTests(TestCase):
    def setUp(self):
        # Sign-up and login are rate-limited per IP; start each test with a fresh limit.
        cache.clear()
        self.addCleanup(cache.clear)
        # Like the browser: every call from the frontend's origin, cookies kept and sent,
        # and CSRF enforced on any request a cookie signs in.
        self.api = APIClient(enforce_csrf_checks=True, HTTP_ORIGIN=FRONTEND)

    def test_the_frontend_may_send_cookies(self):
        response = self.api.get("/api/v1/health/")
        self.assertEqual(response["Access-Control-Allow-Origin"], FRONTEND)
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")

    def test_preflight_allows_the_authorization_header(self):
        response = self.api.options(
            "/api/v1/auth/login/",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization, content-type",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], FRONTEND)
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")
        self.assertIn("authorization", response["Access-Control-Allow-Headers"])

    def test_other_origins_are_not_allowed(self):
        for origin in ("https://www.pythonsmm.com", "https://evil.example", "http://localhost:5173"):
            response = self.api.get("/api/v1/health/", HTTP_ORIGIN=origin)
            self.assertNotIn("Access-Control-Allow-Origin", response, origin)

    def test_sign_up_sign_in_refresh_and_claim_need_no_csrf_token(self):
        # Worst case: a Django admin session in the same browser travels with every call.
        self.api.force_login(make_user(2, is_staff=True))
        self.assertEqual(self.api.post(SIGNUP, payload(3), format="json").status_code, 201)
        make_user(1)
        login = self.api.post("/api/v1/auth/login/", {"email": "life-1@example.com", "password": "TestPassword!9317"},
                              format="json")
        self.assertEqual((login.status_code, login["Access-Control-Allow-Origin"]), (200, FRONTEND))
        refresh = self.api.post("/api/v1/auth/refresh/", {"refresh": login.data["refresh"]}, format="json")
        self.assertEqual(refresh.status_code, 200)
        _, task = make_order_task(1)
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.data['access']}")
        self.assertEqual(self.api.post(f"/api/v1/tasks/{task.pk}/claim/").status_code, 200)
