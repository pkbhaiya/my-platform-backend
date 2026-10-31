"""DEBUG, SECRET_KEY and the JWT signing key from environment variables, failing closed.

DEBUG is on only when DJANGO_DEBUG is "true": a missing or misspelled variable
gives production settings. Production must then provide DJANGO_SECRET_KEY and
DJANGO_JWT_SIGNING_KEY. Development keeps a random secret key in the local .env,
written there the first time Django starts without one.
"""

import ipaddress
import os
import secrets

from django.core.exceptions import ImproperlyConfigured


def debug_from_env(environ):
    return environ.get("DJANGO_DEBUG", "").strip().lower() == "true"


def secret_key_from_env(environ, *, debug, env_file):
    key = environ.get("DJANGO_SECRET_KEY", "").strip()
    if key:
        return key
    if not debug:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY (50+ random characters); it is required unless DJANGO_DEBUG=true.")
    key = secrets.token_urlsafe(64)  # URL-safe: no quoting needed in .env
    try:
        append_env_line(env_file, f"DJANGO_SECRET_KEY={key}")
    except OSError as exc:
        raise ImproperlyConfigured(f"Could not save a development DJANGO_SECRET_KEY to {env_file} ({exc.strerror}); set it yourself.") from exc
    # Child processes (the runserver reloader, parallel tests) inherit it instead of generating another.
    environ["DJANGO_SECRET_KEY"] = key
    return key


def append_env_line(env_file, line):
    """Add one line to a .env file, creating it readable by its owner only."""
    fd = os.open(env_file, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        content = handle.read()
        handle.write(("\n" if content and not content.endswith("\n") else "") + line + "\n")


def jwt_signing_key_from_env(environ, *, debug, secret_key):
    key = environ.get("DJANGO_JWT_SIGNING_KEY", "").strip()
    if key:
        return key
    if debug:
        return secret_key
    raise ImproperlyConfigured("Set DJANGO_JWT_SIGNING_KEY (50+ random characters, not the secret key); it is required unless DJANGO_DEBUG=true.")


def is_local_host(host):
    """localhost (and *.localhost), a loopback address or a private LAN address."""
    name = host.strip().lower()
    if name in ("localhost", ".localhost") or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name.removeprefix("[").removesuffix("]"))
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def refuse_public_hosts_in_debug(hosts):
    """DEBUG shows tracebacks and settings: never serve it under a public host name."""
    public = [host for host in hosts if not is_local_host(host)]
    if public:
        raise ImproperlyConfigured(
            f"DJANGO_DEBUG=true is for local development only, but ALLOWED_HOSTS includes {', '.join(public)}. "
            "Remove it from DJANGO_ALLOWED_HOSTS, or set DJANGO_DEBUG=false."
        )
