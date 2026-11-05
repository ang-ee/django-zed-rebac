"""``rebac.backend()`` selects and caches the local backend."""

from __future__ import annotations

import subprocess
import sys

import pytest
from django.test import override_settings

import rebac.backends
from rebac import LocalBackend, backend


def test_backend_import_does_not_populate_auth_models() -> None:
    script = """
import sys
from django.apps import apps
from django.conf import settings

settings.configure(
    SECRET_KEY="backend-import",
    INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth", "rebac"],
)
assert not apps.apps_ready
from rebac.backends import backend

assert callable(backend)
assert not apps.apps_ready
assert "django.contrib.auth.base_user" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_local_backend_is_the_default_and_is_cached() -> None:
    first = backend()
    assert isinstance(first, LocalBackend)
    assert backend() is first


@pytest.mark.parametrize("kind", ["postgres", "spicedb"])
def test_unknown_backend_value_is_rejected(kind: str) -> None:
    with (
        override_settings(REBAC_BACKEND=kind),
        pytest.raises(ValueError, match=kind),
    ):
        backend()
    assert rebac.backends._backend is None
    assert isinstance(backend(), LocalBackend)
