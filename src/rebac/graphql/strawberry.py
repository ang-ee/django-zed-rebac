"""Strawberry + Channels integration for ``django-zed-rebac``.

Two pieces:

  - :class:`RebacExtension` — Strawberry Schema Extension that brackets
    every GraphQL operation (query, mutation, AND each subscription
    emission) with a fresh :func:`rebac.evaluator.evaluator_scope` +
    :func:`rebac.consistency.zookie_scope`. This is the GraphQL-side
    equivalent of what :class:`rebac.middleware.ActorMiddleware` does
    for plain HTTP.

  - :class:`RebacChannelsConsumerMixin` — mixin for Channels
    consumers carrying GraphQL-over-WebSocket subscriptions. Resolves
    the actor at handshake from ``self.scope["user"]`` and pins it on
    the connection-level :func:`rebac.actors._current_actor`. The
    actor lives for the connection lifetime; the evaluator + Zookie
    scopes inside :class:`RebacExtension` reset per emission so a
    revoked grant takes effect on the next yield.

Subscription invariants:

  - **Actor**: connection-scoped. A long-lived WS started 2h ago
    keeps the actor identity it had at handshake. (Auth re-validation
    is a separate concern from this adapter.)
  - **Evaluator**: per-emission. Revoked grants take effect at the
    next tick, never silently served from a connection-wide cache.
  - **Zookie**: per-emission. Subscriptions are inherently
    write-triggered (the data changed, that's why we emit), so the
    write's Zookie naturally lives within the per-emission scope.

Behind the ``[strawberry]`` extra:

    pip install django-zed-rebac[strawberry]

Importing this module without the extra installed raises a
``ModuleNotFoundError`` with a hint. We don't import
``strawberry.channels`` here — Channels is wired by the consumer's
own ASGI router and the mixin uses only the public ``scope`` /
``connect`` surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strawberry.extensions import SchemaExtension

from ..actors import _current_actor
from ..consistency import current_zookie, zookie_scope
from ..errors import NoActorResolvedError
from ..evaluator import current_evaluator, evaluator_scope

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator


class RebacExtension(SchemaExtension):
    """Per-operation evaluator + Zookie scope for Strawberry schemas.

    Usage::

        import strawberry
        from rebac.graphql.strawberry import RebacExtension

        schema = strawberry.Schema(
            query=Query,
            mutation=Mutation,
            subscription=Subscription,
            extensions=[RebacExtension],
        )

    For each GraphQL operation (HTTP query/mutation OR subscription
    emission) the extension:

    1. Opens a fresh evaluator scope (LRU cache per
       ``REBAC_EVALUATOR_CACHE_SIZE``).
    2. Opens a fresh Zookie scope (no initial token — the SchemaExtension
       runs inside whatever transport scope the surrounding
       middleware/consumer already established).
    3. Mirrors ``current_evaluator()`` onto ``info.context.rebac_evaluator``
       and ``current_zookie()`` onto ``info.context.rebac_zookie`` for
       resolvers that prefer explicit DI over the ambient ContextVar.
       Best-effort — if ``info.context`` doesn't accept attribute
       assignment (e.g. a Mapping), the mirror is silently skipped.

    Composition with :class:`rebac.middleware.ActorMiddleware`: for
    plain HTTP GraphQL the middleware already opens evaluator + zookie
    scopes for the request lifetime, and the extension's per-operation
    scopes nest harmlessly inside. The middleware's scopes still apply
    to non-GraphQL response paths (e.g. extension errors before any
    resolver runs).
    """

    def on_operation(self) -> Iterator[None]:
        """Strawberry ≥0.220 hook fired once per operation.

        Generator form: yields exactly once after entering both scopes;
        teardown runs in the ``finally`` of the surrounding ``with``
        blocks when the schema's execution returns.
        """
        with evaluator_scope():
            with zookie_scope():
                self._mirror_onto_context()
                yield

    def _mirror_onto_context(self) -> None:
        """Best-effort: copy ContextVar values onto ``info.context``.

        Strawberry's ``execution_context.context`` is the same object
        passed as ``info.context`` to resolvers. Some applications use
        a dataclass / pydantic model / plain dict — attribute assignment
        may or may not work. We swallow ``AttributeError`` /
        ``TypeError`` so a read-only context doesn't crash the
        operation; resolvers in that mode just use the ambient
        ContextVar (``current_evaluator()`` / ``current_zookie()``).
        """
        context = getattr(self.execution_context, "context", None)
        if context is None:
            return
        try:
            context.rebac_evaluator = current_evaluator()
            context.rebac_zookie = current_zookie()
        except AttributeError:
            pass
        except TypeError:
            # Read-only or mapping-only context; ambient ContextVar
            # remains the source of truth. Resolvers can call
            # ``current_evaluator()`` directly.
            pass


class RebacChannelsConsumerMixin:
    """Mixin for Channels consumers carrying GraphQL-over-WebSocket.

    Resolves the actor at handshake from ``self.scope["user"]`` and
    pins it on the connection-level :func:`rebac.actors._current_actor`
    ContextVar so every subscription emission sees the same identity.

    Compose with whichever consumer base your stack uses
    (``JsonWebsocketConsumer``, ``AsyncJsonWebsocketConsumer``,
    Strawberry's own ``GraphQLWSConsumer`` /
    ``GraphQLTransportWSConsumer``)::

        from channels.generic.websocket import AsyncJsonWebsocketConsumer
        from strawberry.channels import GraphQLWSConsumer
        from rebac.graphql.strawberry import RebacChannelsConsumerMixin

        class MyGraphQLConsumer(RebacChannelsConsumerMixin, GraphQLWSConsumer):
            pass

    The mixin overrides ``connect`` only; subscribe / send / disconnect
    flow through the underlying consumer unchanged. ``super().connect()``
    is called so other mixins / the base consumer still run.

    For sync consumers, the mixin's ``connect`` is async because
    Channels' WS protocol is async-only — sync ``JsonWebsocketConsumer``
    spawns a thread-local event loop internally and either ``connect``
    shape works.

    The actor is also re-resolved on each subscription emission by the
    :class:`RebacExtension` schema extension; this mixin's contribution
    is the connection-lifetime ``_current_actor`` so the evaluator and
    Zookie scopes opened per emission have an actor to work with.
    """

    scope: dict[str, Any]
    _rebac_actor_token: Any = None

    async def connect(self) -> None:
        from ..actors import to_subject_ref

        user = self.scope.get("user")
        actor = None
        if user is not None:
            try:
                actor = to_subject_ref(user)
            except NoActorResolvedError:
                actor = None
        # Set ambient actor for the connection lifetime; store the
        # token so disconnect can reset cleanly without leaking the
        # ContextVar value into the next request handled by the same
        # event loop.
        self._rebac_actor_token = _current_actor.set(actor)
        await super().connect()  # type: ignore[misc]

    async def disconnect(self, code: int) -> None:
        token = self._rebac_actor_token
        try:
            await super().disconnect(code)  # type: ignore[misc]
        finally:
            if token is not None:
                try:
                    _current_actor.reset(token)
                except ValueError:
                    # Reset fails when the ContextVar has been altered
                    # since set — happens in test harnesses that share
                    # event loops. Silently drop the token; the next
                    # consumer's connect will install its own value.
                    pass
                self._rebac_actor_token = None
