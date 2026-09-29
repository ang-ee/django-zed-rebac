"""pytest-django configuration."""

from __future__ import annotations

import django
import pytest
from django.conf import settings


def pytest_configure() -> None:
    if not settings.configured:
        from . import settings as test_settings  # noqa: F401
    django.setup()


@pytest.fixture(autouse=True)
def isolated_process_state():
    """Keep each worker's test order independent without touching test DB policy.

    pytest-django owns worker DB suffixes; tmp_path owns worker file isolation.
    Registrations made during collection belong to the test application and
    survive, while test-local registrations, schemas and memos do not leak.
    """
    from django.contrib.contenttypes.models import ContentType

    from rebac import actors, consistency, evaluator, resources
    from rebac.backends import reset_backend
    from rebac.caveats import reset_cache
    from rebac.conf import app_settings

    registries = (actors._subject_registry, resources._resource_registry)
    snapshots = [registry.copy() for registry in registries]
    slots = (
        (actors._current_actor, None),
        (actors._sudo_state, None),
        (consistency._current_zookie, consistency._NO_SCOPE),
        (evaluator._current_evaluator, None),
    )
    tokens = [(slot, slot.set(default)) for slot, default in slots]
    reset_backend()
    reset_cache()
    app_settings.reset()
    ContentType.objects.clear_cache()
    try:
        yield
    finally:
        reset_backend()
        reset_cache()
        app_settings.reset()
        ContentType.objects.clear_cache()
        for registry, snapshot in zip(registries, snapshots, strict=True):
            registry.clear()
            registry.update(snapshot)
        for slot, token in reversed(tokens):
            slot.reset(token)
