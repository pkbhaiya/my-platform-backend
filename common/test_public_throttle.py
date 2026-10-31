"""The anonymous public endpoints are rate-limited per client IP (DEFAULT_THROTTLE_RATES["public"])."""

from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from common.checks import check_shared_cache

OVERVIEW = "/api/v1/public/overview/"
CONFIG = "/api/v1/public/config/"
INVITE_CHECK = "/api/v1/public/invite-code/ZZZZZZZZ/"
IP = "198.51.100.7"


class PublicEndpointThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()

    def get(self, url, ip=IP):
        return self.client.get(url, REMOTE_ADDR=ip)

    def test_the_limit_is_60_a_minute(self):
        self.assertEqual(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["public"], "60/minute")

    def test_each_endpoint_allows_60_requests_a_minute_per_ip(self):
        for url in (OVERVIEW, CONFIG):
            with self.subTest(url):
                cache.clear()
                for _ in range(60):
                    self.assertEqual(self.get(url).status_code, 200)
                refused = self.get(url)
                self.assertEqual(refused.status_code, 429)
                self.assertIn("Retry-After", refused)
                self.assertEqual(self.get(url, ip="198.51.100.8").status_code, 200)  # another address has its own limit

    def test_endpoints_count_separately(self):
        for _ in range(60):
            self.get(CONFIG)
        self.assertEqual(self.get(CONFIG).status_code, 429)
        self.assertEqual(self.get(OVERVIEW).status_code, 200)

    def test_the_invite_code_check_keeps_its_stricter_limit_and_gets_the_public_one(self):
        self.assertEqual([self.get(INVITE_CHECK).status_code for _ in range(21)][-2:], [200, 429])  # 20/minute
        cache.clear()
        with patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"invite_code": "1000/minute"}):
            for _ in range(60):
                self.assertEqual(self.get(INVITE_CHECK).status_code, 200)
            self.assertEqual(self.get(INVITE_CHECK).status_code, 429)


class SharedCacheDeployCheckTests(SimpleTestCase):
    LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    REDIS = {"default": {"BACKEND": "django_redis.cache.RedisCache", "LOCATION": "redis://127.0.0.1:6379/1"}}

    def test_production_without_redis_is_warned(self):
        with override_settings(DEBUG=False, CACHES=self.LOCMEM):
            self.assertEqual([warning.id for warning in check_shared_cache(None)], ["common.W001"])

    def test_redis_or_development_is_fine(self):
        with override_settings(DEBUG=False, CACHES=self.REDIS):
            self.assertEqual(check_shared_cache(None), [])
        with override_settings(DEBUG=True, CACHES=self.LOCMEM):
            self.assertEqual(check_shared_cache(None), [])
