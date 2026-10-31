from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.utils.module_loading import import_string

from config.cache import cache_from_env


class CacheFromEnvironmentTests(SimpleTestCase):
    def test_local_memory_without_redis_url(self):
        for env in ({}, {"REDIS_URL": ""}, {"REDIS_URL": "  "}):
            self.assertEqual(cache_from_env(env), {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}, env)

    def test_redis_url_uses_django_redis_with_short_timeouts(self):
        config = cache_from_env({"REDIS_URL": " redis://:secret@cache.internal:6379/1 "})
        self.assertEqual(config["BACKEND"], "django_redis.cache.RedisCache")
        self.assertEqual(config["LOCATION"], "redis://:secret@cache.internal:6379/1")
        self.assertEqual((config["OPTIONS"]["SOCKET_CONNECT_TIMEOUT"], config["OPTIONS"]["SOCKET_TIMEOUT"]), (2, 2))
        # The backend loads and accepts the configuration without connecting.
        import_string(config["BACKEND"])(config["LOCATION"], config)

    def test_tls_and_socket_urls(self):
        for url in ("rediss://cache.internal:6380/0", "unix:///run/redis/redis.sock?db=1"):
            self.assertEqual(cache_from_env({"REDIS_URL": url})["LOCATION"], url)

    def test_other_schemes_are_refused(self):
        for url in ("http://cache.internal:6379", "cache.internal:6379", "memcached://cache:11211"):
            with self.assertRaises(ImproperlyConfigured, msg=url):
                cache_from_env({"REDIS_URL": url})
