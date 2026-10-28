"""The test app's models and migrations agree (a module-level test model needs a migration)."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_test_app_has_no_missing_migrations() -> None:
    out = io.StringIO()
    call_command("makemigrations", "testapp", "--check", "--dry-run", stdout=out, verbosity=1)
    assert "No changes detected" in out.getvalue(), out.getvalue()
