"""Settings of the policy benchmark (``tests.bench``): the stand-in models of one exported policy.

PostgreSQL when ``REBAC_TEST_POSTGRES_URL`` names a database, in-memory SQLite otherwise.
``REBAC_BENCH_POLICY`` names the export under ``tests/bench/policies``, ``REBAC_BENCH_STORAGE``
the relationship storage layout. PostgreSQL's JIT is off unless ``REBAC_BENCH_JIT=on``;
``REBAC_BENCH_TIMEOUT`` (``120s``) bounds each statement.
"""

import os

if os.environ.get("REBAC_TEST_POSTGRES_URL"):
    from .settings_postgres import *  # noqa: F403
    from .settings_postgres import DATABASES

    _options = [] if os.environ.get("REBAC_BENCH_JIT", "off") == "on" else ["-c jit=off"]
    if os.environ.get("REBAC_BENCH_TIMEOUT"):
        # A read past this is reported as a timeout, not waited for: "120s".
        _options.append(f"-c statement_timeout={os.environ['REBAC_BENCH_TIMEOUT']}")
    DATABASES["default"]["OPTIONS"]["options"] = " ".join(_options)
else:
    from .settings import *  # noqa: F403

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "rebac",
    "tests.benchapp",
]
REBAC_TRACKED_MODELS: list[str] = []
REBAC_BENCH_POLICY = os.environ.get("REBAC_BENCH_POLICY", "angee")
REBAC_LOCAL_BACKEND_STORAGE = os.environ.get("REBAC_BENCH_STORAGE", "registry")
