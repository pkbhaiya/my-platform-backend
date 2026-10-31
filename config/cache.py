"""CACHES["default"] from environment variables.

Rate limits (DRF throttles), the public overview response and a few cooldowns
live in this cache. With REDIS_URL set every worker process shares one Redis
cache, so a limit of 10 logins a minute means 10 in total. Without it each
process keeps its own memory cache (fine for development and a single worker).
"""

from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

REDIS_SCHEMES = ("redis", "rediss", "unix")
# Seconds; a stalled Redis fails the request instead of hanging the worker.
REDIS_TIMEOUT_SECONDS = 2


def cache_from_env(environ):
    url = environ.get("REDIS_URL", "").strip()
    if not url:
        return {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    if urlsplit(url).scheme not in REDIS_SCHEMES:
        raise ImproperlyConfigured("REDIS_URL must start with redis://, rediss:// or unix://, e.g. redis://127.0.0.1:6379/1.")
    return {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": url,
        "OPTIONS": {
            "SOCKET_CONNECT_TIMEOUT": REDIS_TIMEOUT_SECONDS,
            "SOCKET_TIMEOUT": REDIS_TIMEOUT_SECONDS,
        },
    }
