"""DATABASES["default"] from environment variables.

Development uses SQLite (the default). Production uses MySQL, configured with
either DATABASE_URL (mysql://user:password@host:3306/name) or the explicit
DB_ENGINE=mysql, DB_NAME, DB_USER, DB_PASSWORD, DB_HOST and DB_PORT variables.
"""

from urllib.parse import unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured

# Applied to every MySQL connection: full Unicode, strict writes (no silent
# truncation), and READ COMMITTED, which the claim/review row locks assume.
MYSQL_OPTIONS = {
    "charset": "utf8mb4",
    "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
    "isolation_level": "read committed",
}


def sqlite_database(path):
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": path}


def mysql_database(*, name, user, password, host, port, conn_max_age):
    if not name:
        raise ImproperlyConfigured("Set the MySQL database name (DB_NAME, or the path of DATABASE_URL).")
    return {
        "ENGINE": "django.db.backends.mysql",
        "NAME": name,
        "USER": user,
        "PASSWORD": password,
        "HOST": host or "127.0.0.1",
        "PORT": str(port or 3306),
        "CONN_MAX_AGE": conn_max_age,
        "OPTIONS": dict(MYSQL_OPTIONS),
    }


def database_from_url(url, *, base_dir, conn_max_age=60):
    """Parse mysql://user:pass@host:port/name or sqlite:///relative/path (sqlite:////absolute/path)."""
    parts = urlsplit(url)
    if parts.query or parts.fragment:
        raise ImproperlyConfigured("DATABASE_URL options are not supported; use DB_CONN_MAX_AGE and the fixed MySQL options.")
    if parts.scheme == "sqlite":
        path = unquote(parts.path)[1:]
        if not path:
            raise ImproperlyConfigured("A sqlite DATABASE_URL needs a file path, e.g. sqlite:///db.sqlite3.")
        return sqlite_database(path if path.startswith("/") else base_dir / path)
    if parts.scheme == "mysql":
        return mysql_database(
            name=unquote(parts.path.lstrip("/")),
            user=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
            host=parts.hostname,
            port=parts.port,
            conn_max_age=conn_max_age,
        )
    raise ImproperlyConfigured("DATABASE_URL must start with mysql:// or sqlite://.")


def database_from_env(environ, base_dir):
    conn_max_age = int(environ.get("DB_CONN_MAX_AGE", "60"))
    url = environ.get("DATABASE_URL", "").strip()
    if url:
        return database_from_url(url, base_dir=base_dir, conn_max_age=conn_max_age)
    engine = environ.get("DB_ENGINE", "sqlite").strip().lower()
    if engine == "sqlite":
        return sqlite_database(base_dir / "db.sqlite3")
    if engine == "mysql":
        return mysql_database(
            name=environ.get("DB_NAME", ""),
            user=environ.get("DB_USER", ""),
            password=environ.get("DB_PASSWORD", ""),
            host=environ.get("DB_HOST", "127.0.0.1"),
            port=environ.get("DB_PORT", "3306"),
            conn_max_age=conn_max_age,
        )
    raise ImproperlyConfigured("DB_ENGINE must be 'sqlite' or 'mysql'.")
