from rest_framework.throttling import SimpleRateThrottle


class PublicEndpointThrottle(SimpleRateThrottle):
    """Requests per client IP to each anonymous public endpoint (DEFAULT_THROTTLE_RATES["public"]).

    Every endpoint counts separately, so a page that calls two of them uses two
    budgets. Keyed by IP whether or not the request is signed in; NUM_PROXIES decides the IP.
    """

    scope = "public"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": f"{self.scope}_{type(view).__name__}", "ident": self.get_ident(request)}
