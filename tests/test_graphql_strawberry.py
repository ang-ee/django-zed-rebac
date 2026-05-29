"""Strawberry / Channels adapter tests.

Unit-mocks the Schema execution context — full Strawberry integration
lives behind a separate ``-m strawberry_integration`` marker so the
default CI matrix doesn't pay the import cost.

Skips entirely when ``strawberry-graphql`` isn't installed (``[strawberry]``
extra not yet wired into the dev install).
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "strawberry",
    reason="strawberry-graphql not installed — install with django-zed-rebac[strawberry]",
)

from rebac import (
    Zookie,
    current_evaluator,
    current_zookie,
    record_zookie,
)
from rebac.graphql.strawberry import (
    RebacChannelsConsumerMixin,
    RebacExtension,
)


class _FakeExecutionContext:
    """Mimics enough of Strawberry's ExecutionContext for the extension.

    Just a context attribute the extension reaches through. Real
    ``ExecutionContext`` carries dozens more fields irrelevant here.
    """

    def __init__(self, context: object | None = None) -> None:
        self.context = context


class _MutableContext:
    """A namespace context — supports attribute assignment.

    The extension writes ``rebac_evaluator`` / ``rebac_zookie`` onto the
    context; declare them so the assertions are type-visible.
    """

    rebac_evaluator: object
    rebac_zookie: object


def _make_extension(context: object | None = None) -> RebacExtension:
    """Build an extension instance with a fake execution_context.

    Strawberry's ``SchemaExtension`` is instantiated by the Schema
    (taking the execution_context as a runtime attribute), so unit
    tests assign it manually rather than going through __init__.
    """
    ext = RebacExtension.__new__(RebacExtension)
    ext.execution_context = _FakeExecutionContext(context)  # type: ignore[assignment]
    return ext


def _run_on_operation(extension: RebacExtension):
    """Drive the extension's ``on_operation`` generator one step."""
    gen = extension.on_operation()
    next(gen)  # enter scopes
    return gen


def _close(gen) -> None:
    """Exhaust the generator so its ``finally`` block runs."""
    try:
        next(gen)
    except StopIteration:
        pass


# ---------- Per-operation scope ----------


def test_extension_opens_evaluator_and_zookie_scopes():
    extension = _make_extension(_MutableContext())
    assert current_evaluator() is None
    assert current_zookie() is None
    gen = _run_on_operation(extension)
    try:
        assert current_evaluator() is not None
        # Zookie initially None per scope (no transport configured).
        assert current_zookie() is None
        record_zookie(Zookie("local", "100"))
        assert current_zookie() == Zookie("local", "100")
    finally:
        _close(gen)
    # Both ContextVars reset on scope exit.
    assert current_evaluator() is None
    assert current_zookie() is None


def test_extension_per_emission_resets_cache():
    """Two emissions = two fresh evaluator scopes; cache from #1 not in #2."""
    ext1 = _make_extension(_MutableContext())
    ext2 = _make_extension(_MutableContext())

    gen1 = _run_on_operation(ext1)
    ev_in_emission_1 = current_evaluator()
    _close(gen1)

    gen2 = _run_on_operation(ext2)
    ev_in_emission_2 = current_evaluator()
    _close(gen2)

    assert ev_in_emission_1 is not None
    assert ev_in_emission_2 is not None
    assert ev_in_emission_1 is not ev_in_emission_2


def test_extension_mirrors_evaluator_onto_info_context():
    """Resolvers preferring DI get ``info.context.rebac_evaluator`` populated."""
    ctx = _MutableContext()
    extension = _make_extension(ctx)
    gen = _run_on_operation(extension)
    try:
        assert ctx.rebac_evaluator is current_evaluator()
        assert ctx.rebac_zookie is None
    finally:
        _close(gen)


def test_extension_skips_mirror_on_readonly_context():
    """A frozen / mapping-only context doesn't crash the operation."""

    class _Frozen:
        __slots__ = ("placeholder",)

        def __setattr__(self, name: str, value: object) -> None:
            if name == "placeholder":
                object.__setattr__(self, name, value)
                return
            raise AttributeError(f"read-only: {name}")

    ctx = _Frozen()
    extension = _make_extension(ctx)
    gen = _run_on_operation(extension)
    try:
        # The mirror silently failed; the extension still entered scopes.
        assert current_evaluator() is not None
    finally:
        _close(gen)


def test_extension_with_none_context_works():
    extension = _make_extension(None)
    gen = _run_on_operation(extension)
    try:
        assert current_evaluator() is not None
    finally:
        _close(gen)


# ---------- RebacChannelsConsumerMixin ----------


@pytest.mark.django_db
def test_consumer_mixin_resolves_actor_at_handshake(monkeypatch):
    """``connect`` pulls user from scope and pins ``_current_actor``."""
    import asyncio

    from django.contrib.auth.models import AnonymousUser

    from rebac.actors import current_actor

    class _Base:
        scope: dict[str, object]

        async def connect(self) -> None:
            # Capture the actor that's ambient at this point.
            self.actor_at_connect = current_actor()

        async def disconnect(self, code: int) -> None:
            pass

    class _Consumer(RebacChannelsConsumerMixin, _Base):
        def __init__(self, scope: dict[str, object]) -> None:
            self.scope = scope

    consumer = _Consumer({"user": AnonymousUser()})
    asyncio.run(consumer.connect())
    actor = consumer.actor_at_connect
    assert actor is not None
    assert actor.subject_type == "auth/anonymous"
    assert actor.subject_id == "*"


def test_consumer_mixin_handles_missing_user_in_scope():
    """No ``scope["user"]`` → actor stays None; no crash."""
    import asyncio

    class _Base:
        scope: dict[str, object]

        async def connect(self) -> None:
            pass

        async def disconnect(self, code: int) -> None:
            pass

    class _Consumer(RebacChannelsConsumerMixin, _Base):
        def __init__(self) -> None:
            self.scope = {}

    consumer = _Consumer()
    asyncio.run(consumer.connect())
    # Cleanup: disconnect resets the (None) token.
    asyncio.run(consumer.disconnect(1000))
