from __future__ import annotations

from typing import Any

import pytest
from django.test import override_settings

from rebac import SubjectRef, sudo
from rebac.errors import MissingActorError, SudoNotAllowedError
from rebac.managers import RebacManager, RebacQuerySet
from tests.testapp.models import Post


class CustomPostQuerySet(RebacQuerySet[Any]):
    def titled(self, title: str) -> CustomPostQuerySet:
        return self.filter(title=title)


def test_rebac_manager_from_queryset_preserves_queryset_class() -> None:
    manager = RebacManager.from_queryset(CustomPostQuerySet)()
    manager.model = Post

    queryset = manager.get_queryset()

    assert isinstance(queryset, CustomPostQuerySet)
    assert queryset.model is Post


def test_rebac_manager_from_queryset_copied_methods_use_custom_queryset() -> None:
    manager = RebacManager.from_queryset(CustomPostQuerySet)()
    manager.model = Post

    queryset = manager.titled("Notes")

    assert isinstance(queryset, CustomPostQuerySet)
    assert str(queryset.query)


def test_with_action_survives_queryset_cloning() -> None:
    manager = RebacManager()
    manager.model = Post

    queryset = manager.with_action("credential_lookup").filter(title="Notes").order_by("id")

    assert queryset._rebac_action == "credential_lookup"


def test_with_action_rejects_empty_action() -> None:
    manager = RebacManager()
    manager.model = Post

    try:
        manager.with_action("")
    except ValueError as exc:
        assert "non-empty action" in str(exc)
    else:
        raise AssertionError("with_action() accepted an empty action")


def test_sudo_denied_when_disabled() -> None:
    manager = RebacManager()
    manager.model = Post

    with override_settings(REBAC_ALLOW_SUDO=False):
        try:
            manager.sudo(reason="request.path")
        except SudoNotAllowedError:
            pass
        else:
            raise AssertionError("sudo() was allowed while REBAC_ALLOW_SUDO=False")


def test_system_context_allowed_when_sudo_disabled() -> None:
    manager = RebacManager()
    manager.model = Post

    with override_settings(REBAC_ALLOW_SUDO=False):
        queryset = manager.system_context(reason="fixture.load")

    assert queryset.is_sudo()


def test_queryset_effective_actor_observer_does_not_raise_without_actor() -> None:
    manager = RebacManager()
    manager.model = Post

    assert manager.get_queryset().effective_actor() == (None, False)


def test_queryset_effective_actor_strict_raises_without_actor() -> None:
    manager = RebacManager()
    manager.model = Post

    with pytest.raises(MissingActorError):
        manager.get_queryset().effective_actor(strict=True)


@pytest.mark.django_db
def test_queryset_pinned_actor_beats_ambient_sudo() -> None:
    manager = RebacManager()
    manager.model = Post
    actor = SubjectRef.of("auth/user", "1")

    with sudo(reason="test.ambient"):
        assert manager.with_actor(actor).effective_actor() == (actor, False)
