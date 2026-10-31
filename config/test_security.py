import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.security import (
    debug_from_env,
    is_local_host,
    jwt_signing_key_from_env,
    refuse_public_hosts_in_debug,
    secret_key_from_env,
)


class DebugFromEnvironmentTests(SimpleTestCase):
    def test_only_true_turns_debug_on(self):
        for value in ("true", "True", " TRUE "):
            self.assertTrue(debug_from_env({"DJANGO_DEBUG": value}), value)
        for env in ({}, {"DJANGO_DEBUG": ""}, {"DJANGO_DEBUG": "false"}, {"DJANGO_DEBUG": "1"}, {"DJANGO_DEBUG": "yes"}, {"DJANGO_DEBUG": "ture"}):
            self.assertFalse(debug_from_env(env), env)


class SecretKeyTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_file = Path(directory.name) / ".env"

    def test_the_environment_key_wins(self):
        self.assertEqual(secret_key_from_env({"DJANGO_SECRET_KEY": " from-env "}, debug=False, env_file=self.env_file), "from-env")
        self.assertFalse(self.env_file.exists())

    def test_production_without_a_key_refuses_to_start(self):
        for env in ({}, {"DJANGO_SECRET_KEY": ""}, {"DJANGO_SECRET_KEY": "   "}):
            with self.assertRaisesMessage(ImproperlyConfigured, "Set DJANGO_SECRET_KEY"):
                secret_key_from_env(env, debug=False, env_file=self.env_file)
        self.assertFalse(self.env_file.exists())

    def test_development_generates_a_key_into_a_new_env_file(self):
        env = {}
        key = secret_key_from_env(env, debug=True, env_file=self.env_file)
        self.assertEqual(self.env_file.read_text(), f"DJANGO_SECRET_KEY={key}\n")
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(env["DJANGO_SECRET_KEY"], key)
        # Long and random enough for Django's own deploy check (security.W009).
        self.assertGreaterEqual(len(key), 50)
        self.assertGreaterEqual(len(set(key)), 5)
        self.assertFalse(key.startswith("django-insecure-"))
        self.assertEqual(secret_key_from_env(env, debug=True, env_file=self.env_file), key)
        self.assertEqual(self.env_file.read_text().count("DJANGO_SECRET_KEY="), 1)

    def test_development_keeps_existing_lines(self):
        self.env_file.write_text("DJANGO_DEBUG=true\nOTHER=1")  # no final newline
        key = secret_key_from_env({}, debug=True, env_file=self.env_file)
        self.assertEqual(self.env_file.read_text(), f"DJANGO_DEBUG=true\nOTHER=1\nDJANGO_SECRET_KEY={key}\n")

    def test_every_generated_key_is_different(self):
        self.assertNotEqual(
            secret_key_from_env({}, debug=True, env_file=self.env_file),
            secret_key_from_env({}, debug=True, env_file=self.env_file),
        )

    def test_an_unwritable_env_file_is_explained(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "Could not save a development DJANGO_SECRET_KEY"):
            secret_key_from_env({}, debug=True, env_file=self.env_file.parent / "missing-dir" / ".env")


class JwtSigningKeyTests(SimpleTestCase):
    def test_the_environment_key_wins(self):
        for debug in (True, False):
            self.assertEqual(jwt_signing_key_from_env({"DJANGO_JWT_SIGNING_KEY": "jwt-key"}, debug=debug, secret_key="s"), "jwt-key")

    def test_development_falls_back_to_the_secret_key(self):
        self.assertEqual(jwt_signing_key_from_env({}, debug=True, secret_key="secret"), "secret")

    def test_production_requires_its_own_key(self):
        for env in ({}, {"DJANGO_JWT_SIGNING_KEY": " "}):
            with self.assertRaisesMessage(ImproperlyConfigured, "Set DJANGO_JWT_SIGNING_KEY"):
                jwt_signing_key_from_env(env, debug=False, secret_key="secret")


class DebugHostGuardTests(SimpleTestCase):
    LOCAL = ["localhost", "LOCALHOST", "app.localhost", ".localhost", "127.0.0.1", "127.0.1.1", "[::1]",
             "10.0.0.2", "172.16.5.4", "172.31.255.1", "192.168.0.108", "[fd00::1]"]
    PUBLIC = ["api.example.com", "*", ".example.com", "8.8.8.8", "172.32.0.1", "[2001:4860::8888]", "localhost.example.com"]

    def test_local_and_private_hosts_are_local(self):
        for host in self.LOCAL:
            self.assertTrue(is_local_host(host), host)
        for host in self.PUBLIC:
            self.assertFalse(is_local_host(host), host)

    def test_debug_with_a_public_host_refuses_to_start(self):
        refuse_public_hosts_in_debug(self.LOCAL)
        with self.assertRaisesMessage(ImproperlyConfigured, "ALLOWED_HOSTS includes api.example.com, *"):
            refuse_public_hosts_in_debug(["localhost", "api.example.com", "*"])


class SettingsStartupTests(SimpleTestCase):
    """config/settings.py loaded in a fresh process, as at a real start."""

    def run_settings(self, **overrides):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # Explicit values win over the local .env (load_dotenv never overrides), "" counts as unset.
        env = {**os.environ, "DATABASE_URL": f"sqlite:///{directory.name}/unused.sqlite3",
               "DJANGO_ALLOWED_HOSTS": "", "DJANGO_JWT_SIGNING_KEY": "", **overrides}
        code = ("import django; django.setup(); from django.conf import settings as s; "
                "print(s.DEBUG, s.SIMPLE_JWT['SIGNING_KEY'] == s.SECRET_KEY, s.SECURE_HSTS_SECONDS, s.SECURE_HSTS_INCLUDE_SUBDOMAINS)")
        return subprocess.run(
            [sys.executable, "-c", code], cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=60,
            env={**env, "DJANGO_SETTINGS_MODULE": "config.settings", "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def production(self, **overrides):
        return self.run_settings(**{
            "DJANGO_DEBUG": "false", "DJANGO_SECRET_KEY": "p" * 60, "DJANGO_JWT_SIGNING_KEY": "j" * 60,
            "ACCOUNT_SIGNAL_SALT": "s" * 40, "DJANGO_ALLOWED_HOSTS": "api.example.com", **overrides,
        })

    def test_production_settings_load_with_a_separate_jwt_key(self):
        result = self.production()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["False", "False", "3600", "False"])

    def test_hsts_is_set_from_the_environment(self):
        result = self.production(DJANGO_SECURE_HSTS_SECONDS="31536000", DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS="true")
        self.assertEqual(result.stdout.split(), ["False", "False", "31536000", "True"], result.stderr)
        # An empty value in .env means "not set", not a crash.
        self.assertEqual(self.production(DJANGO_SECURE_HSTS_SECONDS="").stdout.split()[2], "3600")

    def test_development_sends_no_hsts_and_reuses_the_secret_key_for_tokens(self):
        result = self.run_settings(DJANGO_DEBUG="true", DJANGO_SECRET_KEY="d" * 60, DJANGO_SECURE_HSTS_SECONDS="31536000")
        self.assertEqual(result.stdout.split(), ["True", "True", "0", "False"], result.stderr)

    def test_production_without_a_secret_key_refuses_to_start(self):
        result = self.production(DJANGO_SECRET_KEY="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set DJANGO_SECRET_KEY", result.stderr)

    def test_production_without_a_jwt_signing_key_refuses_to_start(self):
        result = self.production(DJANGO_JWT_SIGNING_KEY="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set DJANGO_JWT_SIGNING_KEY", result.stderr)

    def test_debug_with_a_public_allowed_host_refuses_to_start(self):
        result = self.run_settings(DJANGO_DEBUG="true", DJANGO_SECRET_KEY="d" * 60, DJANGO_ALLOWED_HOSTS="api.example.com")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_DEBUG=true is for local development only", result.stderr)
