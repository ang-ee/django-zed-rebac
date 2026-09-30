"""Test settings for pytest-django."""

from __future__ import annotations

SECRET_KEY = "test-secret-key-not-for-production"
DEBUG = True

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "rebac",
    "tests.testapp",
]

AUTHENTICATION_BACKENDS = [
    "rebac.backends.auth.RebacBackend",
    "django.contrib.auth.backends.ModelBackend",
]

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Make tests deterministic — strict mode ON.
REBAC_BACKEND = "local"
REBAC_STRICT_MODE = True
REBAC_REQUIRE_SUDO_REASON = True
REBAC_ALLOW_SUDO = True
REBAC_SUPERUSER_BYPASS = False

# Exercise third-party-style tracking separately from first-party owners.
REBAC_TRACKED_MODELS = ["testapp.BackingStage"]
