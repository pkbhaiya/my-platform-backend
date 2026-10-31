from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.database import MYSQL_OPTIONS, database_from_env, database_from_url

BASE = Path("/srv/app")


class DatabaseFromEnvironmentTests(SimpleTestCase):
    def test_sqlite_is_the_default(self):
        self.assertEqual(database_from_env({}, BASE), {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE / "db.sqlite3"})

    def test_mysql_url_with_an_encoded_password(self):
        config = database_from_env({"DATABASE_URL": "mysql://app:p%40ss%3Aword@db.internal:3307/platform", "DB_CONN_MAX_AGE": "30"}, BASE)
        self.assertEqual(config["ENGINE"], "django.db.backends.mysql")
        self.assertEqual((config["NAME"], config["USER"], config["PASSWORD"]), ("platform", "app", "p@ss:word"))
        self.assertEqual((config["HOST"], config["PORT"], config["CONN_MAX_AGE"]), ("db.internal", "3307", 30))
        self.assertEqual(config["OPTIONS"], MYSQL_OPTIONS)
        self.assertEqual(config["OPTIONS"]["isolation_level"], "read committed")
        self.assertEqual(config["OPTIONS"]["charset"], "utf8mb4")

    def test_mysql_url_defaults_host_and_port(self):
        config = database_from_url("mysql://app:secret@/platform", base_dir=BASE)
        self.assertEqual((config["HOST"], config["PORT"]), ("127.0.0.1", "3306"))

    def test_explicit_mysql_variables(self):
        config = database_from_env({"DB_ENGINE": "mysql", "DB_NAME": "platform", "DB_USER": "app", "DB_PASSWORD": "x", "DB_HOST": "db", "DB_PORT": "3306"}, BASE)
        self.assertEqual((config["NAME"], config["HOST"], config["OPTIONS"]["init_command"]), ("platform", "db", "SET sql_mode='STRICT_TRANS_TABLES'"))

    def test_sqlite_urls(self):
        self.assertEqual(database_from_url("sqlite:///data/dev.sqlite3", base_dir=BASE)["NAME"], BASE / "data/dev.sqlite3")
        self.assertEqual(database_from_url("sqlite:////var/lib/app.sqlite3", base_dir=BASE)["NAME"], "/var/lib/app.sqlite3")

    def test_invalid_configuration_is_refused(self):
        for env in ({"DATABASE_URL": "postgres://u:p@h/db"}, {"DATABASE_URL": "mysql://u:p@h/"},
                    {"DATABASE_URL": "mysql://u:p@h/db?ssl=1"}, {"DB_ENGINE": "oracle"}, {"DB_ENGINE": "mysql"}):
            with self.assertRaises(ImproperlyConfigured, msg=env):
                database_from_env(env, BASE)
