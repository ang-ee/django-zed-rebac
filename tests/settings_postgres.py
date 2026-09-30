"""PostgreSQL suite settings; each pytest worker owns its test database."""

import os
from urllib.parse import parse_qsl, unquote, urlsplit

from .settings import *  # noqa: F403

_database_url = urlsplit(os.environ["REBAC_TEST_POSTGRES_URL"])
if _database_url.scheme not in {"postgres", "postgresql"}:
    raise ValueError("REBAC_TEST_POSTGRES_URL must be a PostgreSQL URL")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(_database_url.path.lstrip("/")),
        "USER": unquote(_database_url.username or ""),
        "PASSWORD": unquote(_database_url.password or ""),
        "HOST": _database_url.hostname or "",
        "PORT": str(_database_url.port or 5432),
        "OPTIONS": dict(parse_qsl(_database_url.query)),
    }
}
