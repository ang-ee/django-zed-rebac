"""PostgreSQL suite settings; each pytest worker owns its test database.

``REBAC_TEST_POSTGRES_BINDING`` picks how psycopg binds parameters: ``client`` (the default,
Django's) interpolates them into the statement text; ``server`` turns on Django's
``server_side_binding`` and ``prepare_threshold = 0``, so every statement is prepared on the
server with its parameters sent apart, and PostgreSQL must infer each parameter's type.
"""

import os
from urllib.parse import parse_qsl, unquote, urlsplit

from .settings import *  # noqa: F403

_database_url = urlsplit(os.environ["REBAC_TEST_POSTGRES_URL"])
if _database_url.scheme not in {"postgres", "postgresql"}:
    raise ValueError("REBAC_TEST_POSTGRES_URL must be a PostgreSQL URL")
_options: dict[str, object] = dict(parse_qsl(_database_url.query))
if {"server_side_binding", "prepare_threshold"} & _options.keys():
    # Django reads typed values (server_side_binding is True), which a URL cannot carry.
    raise ValueError("Set REBAC_TEST_POSTGRES_BINDING=server, not binding options in the URL")

_binding = os.environ.get("REBAC_TEST_POSTGRES_BINDING", "client")
if _binding == "server":
    _options |= {"server_side_binding": True, "prepare_threshold": 0}
elif _binding != "client":
    raise ValueError("REBAC_TEST_POSTGRES_BINDING must be 'client' or 'server'")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(_database_url.path.lstrip("/")),
        "USER": unquote(_database_url.username or ""),
        "PASSWORD": unquote(_database_url.password or ""),
        "HOST": _database_url.hostname or "",
        "PORT": str(_database_url.port or 5432),
        "OPTIONS": _options,
    }
}
