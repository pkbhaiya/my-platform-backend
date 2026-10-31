import uuid

from django.conf import settings

from .identity import DEVICE_COOKIE, valid_device_id

DEVICE_COOKIE_MAX_AGE = 2 * 365 * 24 * 60 * 60


class DeviceIdMiddleware:
    """Give each browser a random device ID in a long-lived httpOnly cookie.

    Only /api/v1/ requests get one (the provider API and Django admin do not).
    The ID is random, so it identifies a browser, not a person; sign-up and
    login store only its salted hash (accounts.identity.record_request_signals).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        cookie = request.COOKIES.get(DEVICE_COOKIE)
        new_id = None
        if valid_device_id(cookie):
            request.device_id = cookie
        elif request.path.startswith("/api/v1/"):
            request.device_id = new_id = str(uuid.uuid4())
        else:
            request.device_id = None
        response = self.get_response(request)
        if new_id:
            response.set_cookie(
                DEVICE_COOKIE, new_id, max_age=DEVICE_COOKIE_MAX_AGE, path="/api/v1/",
                httponly=True, secure=settings.AUTH_COOKIE_SECURE, samesite="Lax",
            )
        return response
