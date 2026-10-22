from __future__ import annotations

import operator
from typing import Any

import pytest
from django.db import models
from django.test import override_settings

from rebac import SubjectRef, sudo
from rebac.errors import MissingActorError, SudoNotAllowedError
from rebac.managers import RebacManager, RebacQuerySet
from tests.testapp.models import Post


class CustomPostQuerySet(RebacQuerySet[Any]):
    def titled(self, title: str) -> CustomPostQuerySet:
        return self.filter(title=title)


def test_documented_manager_imports_resolve_from_package() -> None:
    import rebac

    assert rebac.RebacManager is RebacManager
    assert rebac.RebacQuerySet is RebacQuerySet
    assert {"RebacManager", "RebacQuerySet"} <= set(rebac.__all__)


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


@pytest.mark.parametrize("operation", [operator.and_, operator.or_, operator.xor])
def test_boolean_combinations_preserve_same_custom_queryset(operation) -> None:
    actor = SubjectRef.of("auth/user", "left")
    left = CustomPostQuerySet(model=Post).with_actor(actor)
    right = CustomPostQuerySet(model=Post).with_actor(SubjectRef.of("auth/user", "right"))

    for first, second in ((left, right), (left.none(), right), (left, right.none())):
        combined = operation(first, second)
        assert type(combined) is CustomPostQuerySet
        assert combined is not first
        assert combined is not second
        assert combined.actor() == actor
        assert type(combined.titled("Notes")) is CustomPostQuerySet
    assert right.actor() == SubjectRef.of("auth/user", "right")


@pytest.mark.parametrize("operation", [operator.and_, operator.or_, operator.xor])
def test_empty_boolean_combinations_preserve_django_right_operand_class(operation) -> None:
    class OtherPostQuerySet(RebacQuerySet[Post]):
        pass

    actor = SubjectRef.of("auth/user", "left")
    left = CustomPostQuerySet(model=Post).with_actor(actor)
    right = OtherPostQuerySet(model=Post).with_actor(SubjectRef.of("auth/user", "right"))
    first, second = (left, right.none()) if operation is operator.and_ else (left.none(), right)

    combined = operation(first, second)

    assert type(combined) is OtherPostQuerySet
    assert combined is not first
    assert combined is not second
    assert combined.actor() == actor
    assert right.actor() == SubjectRef.of("auth/user", "right")


def test_manager_dispatches_the_custom_queryset_actor_override() -> None:
    calls = []

    class ActorPostQuerySet(RebacQuerySet[Post]):
        def with_actor(self, actor: Any) -> ActorPostQuerySet:
            calls.append(actor)
            return super().with_actor(actor)

    manager = RebacManager.from_queryset(ActorPostQuerySet)()
    manager.model = Post
    actor = SubjectRef.of("auth/user", "custom-actor")

    queryset = manager.with_actor(actor)

    assert type(queryset) is ActorPostQuerySet
    assert queryset.model is Post
    assert queryset.actor() == actor
    assert calls == [actor]


def test_raw_rejects_a_custom_getter_without_rebac_policy() -> None:
    class PlainGetterManager(RebacManager[Post]):
        def get_queryset(self) -> models.QuerySet[Post]:
            return models.QuerySet(model=self.model)

    manager = PlainGetterManager()
    manager.model = Post

    with pytest.raises(AssertionError, match=r"RebacManager\.raw requires a RebacQuerySet"):
        manager.raw("SELECT 1")


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
