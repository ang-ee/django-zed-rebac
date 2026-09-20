"""Public ``require_permission`` argument-binding and precedence contracts."""

from __future__ import annotations

from typing import Any

import pytest

import rebac
from rebac import CheckResult, NoActorResolvedError, ObjectRef, PermissionDenied, SubjectRef
from rebac.actors import actor_context
from rebac.decorators import require_permission


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def check_access(self, **kwargs: Any) -> CheckResult:
        self.calls.append(kwargs)
        return CheckResult.has() if kwargs["subject"].subject_id == "allowed" else CheckResult.no()


@pytest.fixture
def permission_backend(monkeypatch: pytest.MonkeyPatch) -> RecordingBackend:
    backend = RecordingBackend()
    monkeypatch.setattr(rebac, "backend", lambda: backend)
    monkeypatch.setattr(
        "rebac.decorators.to_object_ref",
        lambda value: ObjectRef("documents/document", value.resource_id),
    )
    return backend


class Resource:
    def __init__(self, resource_id: str) -> None:
        self.resource_id = resource_id


def test_named_actor_and_resource_bind_positional_and_keyword_arguments(
    permission_backend: RecordingBackend,
) -> None:
    calls: list[str] = []

    @require_permission("read", actor_arg="actor", resource_arg="document")
    def read(document: Resource, actor: SubjectRef) -> str:
        calls.append(document.resource_id)
        return document.resource_id

    actor = SubjectRef.of("auth/user", "allowed")
    assert read(Resource("positional"), actor) == "positional"
    assert read(actor=actor, document=Resource("keyword")) == "keyword"
    assert calls == ["positional", "keyword"]
    assert [call["resource"] for call in permission_backend.calls] == [
        ObjectRef("documents/document", "positional"),
        ObjectRef("documents/document", "keyword"),
    ]


def test_bound_method_never_mistakes_self_for_the_named_resource(
    permission_backend: RecordingBackend,
) -> None:
    class Reader:
        @require_permission("read", actor_arg="actor", resource_arg="document")
        def read(self, document: Resource, actor: SubjectRef) -> str:
            return document.resource_id

    assert Reader().read(Resource("method"), SubjectRef.of("auth/user", "allowed")) == "method"
    assert permission_backend.calls[-1]["resource"] == ObjectRef("documents/document", "method")


def test_class_and_static_methods_bind_their_declared_parameters(
    permission_backend: RecordingBackend,
) -> None:
    actor = SubjectRef.of("auth/user", "allowed")

    class Reader:
        @classmethod
        @require_permission("read", actor_arg="actor", resource_arg="document")
        def class_read(cls, document: Resource, actor: SubjectRef) -> tuple[type[Reader], str]:
            return cls, document.resource_id

        @staticmethod
        @require_permission("read", actor_arg="actor", resource_arg="document")
        def static_read(document: Resource, actor: SubjectRef) -> str:
            return document.resource_id

    assert Reader.class_read(Resource("class"), actor) == (Reader, "class")
    assert Reader.static_read(Resource("static"), actor) == "static"
    assert [call["resource"].resource_id for call in permission_backend.calls] == [
        "class",
        "static",
    ]


def test_signature_defaults_are_bound_for_named_arguments(
    permission_backend: RecordingBackend,
) -> None:
    default_actor = SubjectRef.of("auth/user", "allowed")
    default_resource = Resource("default")

    @require_permission("read", actor_arg="actor", resource_arg="document")
    def read(document: Resource = default_resource, actor: SubjectRef = default_actor) -> str:
        return document.resource_id

    assert read() == "default"
    assert permission_backend.calls[-1]["subject"] == default_actor


def test_explicit_actor_is_checked_even_during_ambient_sudo(
    permission_backend: RecordingBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_calls = 0

    @require_permission(
        "read",
        resource_type="documents/document",
        resource_id="fixed",
        actor_arg="actor",
    )
    def read(actor: SubjectRef) -> None:
        nonlocal body_calls
        body_calls += 1

    monkeypatch.setattr("rebac.decorators.is_sudo", lambda: True)
    with pytest.raises(PermissionDenied):
        read(SubjectRef.of("auth/user", "denied"))
    assert body_calls == 0

    read(SubjectRef.of("auth/user", "allowed"))
    assert body_calls == 1
    assert len(permission_backend.calls) == 2


def test_explicit_none_never_falls_back_to_ambient_actor(
    permission_backend: RecordingBackend,
) -> None:
    @require_permission("read", resource_type="documents/document", actor_arg="actor")
    def read(actor: SubjectRef | None) -> None:
        raise AssertionError("body must not run")

    with actor_context(SubjectRef.of("auth/user", "allowed")):
        with pytest.raises(NoActorResolvedError, match="resolved to None"):
            read(None)
    assert permission_backend.calls == []


def test_ambient_actor_and_sudo_remain_the_fallback_without_actor_arg(
    permission_backend: RecordingBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_calls = 0

    @require_permission("read", resource_type="documents/document", resource_id="fixed")
    def read() -> None:
        nonlocal body_calls
        body_calls += 1

    with actor_context(SubjectRef.of("auth/user", "allowed")):
        read()
    monkeypatch.setattr("rebac.decorators.is_sudo", lambda: True)
    read()
    assert body_calls == 2
    assert len(permission_backend.calls) == 1


def test_named_arguments_must_exist_on_the_decorated_signature() -> None:
    with pytest.raises(ValueError, match=r"actor_arg='missing'.*not a parameter"):

        @require_permission("read", resource_type="documents/document", actor_arg="missing")
        def read() -> None:
            pass

    with pytest.raises(ValueError, match=r"resource_arg='missing'.*not a parameter"):

        @require_permission("read", resource_arg="missing")
        def write() -> None:
            pass
