from django.conf import settings
from django.core.checks import Tags, Warning, register

LOCAL_MEMORY_CACHE = "django.core.cache.backends.locmem.LocMemCache"


@register(Tags.security, deploy=True)
def check_shared_cache(app_configs, **kwargs):
    """Production rate limits need one cache for every worker (config/cache.py)."""
    if settings.DEBUG or settings.CACHES["default"]["BACKEND"] != LOCAL_MEMORY_CACHE:
        return []
    return [Warning(
        "REDIS_URL is not set, so rate limits (login, sign-up, public endpoints, /api/v2) are counted "
        "separately in each worker process.",
        hint="Set REDIS_URL, e.g. redis://127.0.0.1:6379/1 (PRODUCTION_SETUP.md section 9).",
        id="common.W001",
    )]
