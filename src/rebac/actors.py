"""Actor resolution: ContextVar, Subject conversion, sudo() / system_context().

The ContextVar `_current_actor` is the *ambient* actor (populated by middleware
and Celery prerun hooks). It is read-only at call sites — `set_current_actor`
should only be invoked at framework boundaries.

Per-queryset actors take strict priority over the ContextVar. See
ARCHITECTURE.md § Three actor-resolution paths.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from importlib import import_module
from typing import Any

from django.contrib.auth import get_user_model

from ._id import subject_id_attr, subject_relation, type_with_prefix
from .conf import app_settings
from .errors import (
    NoActorResolvedError,
    SudoNotAllowedError,
    SudoReasonRequiredError,
)
from .resources import model_resource_type, to_object_ref
from .types import ObjectRef, SubjectRef

# ---------- ContextVar ----------

_current_actor: ContextVar[SubjectRef | None] = ContextVar("rebac_current_actor", default=None)
_sudo_state: ContextVar[dict[str, Any] | None] = ContextVar("rebac_sudo", default=None)


def current_actor() -> SubjectRef | None:
    """The ambient actor. None when no middleware / task hook has populated it.

    Note: ``None`` means "the resolver chain has not run for this scope" — a
    framework error, distinct from "the request is unauthenticated." An
    unauthenticated request is represented by the anonymous SubjectRef
    (:data:`ANONYMOUS_ACTOR`); the default resolver returns it for any
    request whose ``user.is_authenticated`` is False. See
    :func:`is_anonymous_actor`.
    """
    return _current_actor.get()


def set_current_actor(actor: SubjectRef | None) -> None:
    """Mutate the ambient actor.

    Call only at framework boundaries (middleware, Celery prerun, MCP entry).
    Application code should pass the actor through the queryset, not via this.
    """
    _current_actor.set(actor)


# ---------- Anonymous subject ----------
#
# The "anonymous actor" is a real SubjectRef with a stable, settings-driven
# subject_type (``REBAC_ANONYMOUS_TYPE``, default ``"auth/anonymous"``) and
# subject_id ``"*"``. Schemas reference it two ways:
#
#   relation public: auth/anonymous:*        // wildcard subject in a relation type union
#   permission read = viewer + anonymous     // bare schema keyword in a permission expression
#
# Both match the same subject at check time. The default resolver returns
# this SubjectRef for unauthenticated requests so anonymous-readable
# permissions evaluate correctly without callers having to construct it.


def anonymous_actor() -> SubjectRef:
    """The canonical anonymous SubjectRef built from ``REBAC_ANONYMOUS_TYPE``.

    Prefer this over :data:`ANONYMOUS_ACTOR` in code that may run after the
    consumer overrides ``REBAC_ANONYMOUS_TYPE`` — this function reads the
    current setting each call.
    """
    return SubjectRef.of(type_with_prefix(app_settings.REBAC_ANONYMOUS_TYPE), "*")


# Module-level convenience constant. Uses the default ``REBAC_ANONYMOUS_TYPE``
# and no ``REBAC_TYPE_PREFIX`` at import time, so it is only the anonymous actor
# (per :func:`is_anonymous_actor`) in deployments that keep both defaults;
# consumers that override either setting must call :func:`anonymous_actor`.
ANONYMOUS_ACTOR: SubjectRef = SubjectRef.of("auth/anonymous", "*")


def is_anonymous_actor(subject: SubjectRef | None) -> bool:
    """Return True if ``subject`` is the anonymous SubjectRef.

    Reads ``REBAC_ANONYMOUS_TYPE`` so consumer-overridden subject types are
    honoured. Returns False for ``None`` — that's "resolver did not run",
    not "anonymous" (see :func:`current_actor`).
    """
    if subject is None:
        return False
    return (
        subject.subject_type == type_with_prefix(app_settings.REBAC_ANONYMOUS_TYPE)
        and subject.subject_id == "*"
        and not subject.optional_relation
    )


def is_sudo() -> bool:
    return _sudo_state.get() is not None


def current_sudo_reason() -> str | None:
    state = _sudo_state.get()
    return None if state is None else state.get("reason")


# ---------- Subject conversion ----------


ActorLike = SubjectRef | Any
"""Anything resolvable to a `SubjectRef`.

Concretely accepts:
  - `SubjectRef` (passed through)
  - A REBAC-bound Django model (type/id from its object identity and an optional
    `Meta.rebac_subject_relation`)
  - Legacy Django `User` instance (→ configured user type/id)
  - Legacy Django `Group` instance (→ configured group type/id `#member`)
  - Any class decorated with `@rebac_subject(...)` (→ `<type>:<id_attr_value>`)
"""


_subject_registry: dict[type, tuple[str, str]] = {}
"""Mapping `cls -> (rebac_type, id_attr)` populated by `@rebac_subject`."""


def model_can_resolve_subject(model_cls: type[Any]) -> bool:
    """Whether instances of ``model_cls`` can use the canonical subject resolver.

    This is the non-raising class-level preflight for global lifecycle hooks.
    It deliberately shares the resolver's owning metadata instead of trying to
    resolve an instance or maintaining a second registry.
    """
    from django.contrib.auth.models import Group

    if model_resource_type(model_cls):
        return True
    if issubclass(model_cls, (get_user_model(), Group)):
        return True
    return any(issubclass(model_cls, registered) for registered in _subject_registry)


def rebac_subject(*, type: str, id_attr: str = "pk") -> Callable[[type], type]:
    """Decorator: register a class as a subject type.

    Example:
        @rebac_subject(type="auth/apikey", id_attr="public_id")
        class ApiKey: ...
    """
    rebac_type = type
    rebac_id_attr = id_attr

    def _decorator(cls: builtins.type) -> builtins.type:
        _subject_registry[cls] = (rebac_type, rebac_id_attr)
        return cls

    return _decorator


def to_subject_ref(actor: ActorLike) -> SubjectRef:
    """Resolve `actor` to a `SubjectRef`. Raises `NoActorResolvedError`.

    Django's ``AnonymousUser`` resolves to the anonymous SubjectRef
    (``REBAC_ANONYMOUS_TYPE:*``) — see :func:`is_anonymous_actor`. Passing
    raw ``None`` is a framework error (the resolver chain failed) and still
    raises.
    """
    # Imported inside the function, not at module top: ``actors`` is
    # imported during ``INSTALLED_APPS`` boot (via ``rebac/__init__``),
    # and ``django.contrib.auth.models`` cannot be imported until the
    # app registry is ready (AppRegistryNotReady otherwise).
    from django.contrib.auth.models import AnonymousUser, Group

    if actor is None:
        raise NoActorResolvedError("Cannot resolve None to a SubjectRef")
    if isinstance(actor, SubjectRef):
        return actor

    # AnonymousUser inherits from ``object``, not the swappable user model,
    # so it bypasses the isinstance(user_model) check below. Handle it
    # explicitly first — anonymous reads are a first-class authorization
    # path, not an error.
    if isinstance(actor, AnonymousUser):
        return anonymous_actor()

    user_model = get_user_model()
    if isinstance(actor, user_model):
        # Strict-by-default (CLAUDE.md § 3): a user-model instance with
        # ``is_authenticated == False`` is almost always a programming bug
        # (forgot to save, deleted user re-used, mid-test fixture). Anonymous
        # has a dedicated singleton (``AnonymousUser``) — silently downgrading
        # to it would mask the bug class the strict posture is meant to
        # surface. The request-path resolver (:func:`default_resolver`)
        # remains the fail-safe via its ``except NoActorResolvedError`` path.
        if actor.pk is None or not getattr(actor, "is_authenticated", False):
            raise NoActorResolvedError(
                f"{type(actor).__name__} instance is unsaved or has is_authenticated=False. "
                "Pass AnonymousUser explicitly for the anonymous actor, or "
                "save/load a real user row."
            )
        if model_resource_type(user_model):
            resource = to_object_ref(actor)
            return SubjectRef(resource, subject_relation(user_model))
        attr = subject_id_attr(user_model)
        return SubjectRef.of(
            type_with_prefix(app_settings.REBAC_USER_TYPE),
            str(getattr(actor, attr)),
        )

    if model_resource_type(type(actor)):
        if actor.pk is None:
            # A principal must be a persisted row, as for the User branch;
            # an unsaved instance would otherwise resolve to the id "None".
            raise NoActorResolvedError(
                f"{type(actor).__name__} instance is unsaved; save it before using it as a subject."
            )
        resource = to_object_ref(actor)
        return SubjectRef(resource, subject_relation(type(actor)))

    if isinstance(actor, Group):
        attr = subject_id_attr(Group)
        return SubjectRef.of(
            type_with_prefix(app_settings.REBAC_GROUP_TYPE),
            str(getattr(actor, attr)),
            "member",
        )

    # @rebac_subject-registered?
    for cls, (type_, id_attr) in _subject_registry.items():
        if isinstance(actor, cls):
            value = getattr(actor, id_attr)
            return SubjectRef.of(type_with_prefix(type_), str(value))

    raise NoActorResolvedError(
        f"Cannot resolve {type(actor).__name__} instance to SubjectRef. "
        f"Decorate the class with @rebac_subject(type=..., id_attr=...) "
        f"or pass a SubjectRef directly."
    )


def grant_subject_ref(agent: Any, on_behalf_of: Any | None) -> SubjectRef:
    """Build a Grant subject for `agent` acting on behalf of `on_behalf_of`.

    Resolve the agent and user through ``to_subject_ref`` and construct a
    deterministic ``agents/grant:<user-id>.<agent-id>#valid`` subject. The
    application owns the compatible ``valid`` relation and grant relationships;
    this helper neither registers a grant type nor creates its permissions.

    For persisted grants, configure ``REBAC_ACTOR_RESOLVER`` to resolve trusted
    request context to the grant's canonical ``SubjectRef``.
    """
    agent_ref = to_subject_ref(agent)
    if on_behalf_of is None:
        # Standalone agent run — no user-grant intersection.
        return agent_ref
    user_ref = to_subject_ref(on_behalf_of)
    grant_id = f"{user_ref.subject_id}.{agent_ref.subject_id}"
    return SubjectRef(
        object=ObjectRef(type_with_prefix("agents/grant"), grant_id),
        optional_relation="valid",
    )


# ---------- sudo / system_context ----------


@contextmanager
def actor_context(actor: ActorLike) -> Iterator[None]:
    """Block-scoped ambient actor. Usually you want `.with_actor(actor)` on a
    queryset instead — this is for non-queryset code paths (manual checks).
    """
    ref = to_subject_ref(actor) if not isinstance(actor, SubjectRef) else actor
    token = _current_actor.set(ref)
    try:
        yield
    finally:
        _current_actor.reset(token)


def _install_sudo_state(reason: str | None) -> tuple[Any, dict[str, Any], SubjectRef | None]:
    """Reason-check + install the sudo ContextVar slot.

    Shared between :func:`_sudo_state_context` and
    :func:`_asudo_state_context`. Returns the reset token, the state
    dict (used by the caller to pass ``reason`` into the audit emit),
    and the ambient actor captured at sudo entry — that's the subject
    the bypass is being applied "as". For v1 origin == actor (no
    impersonation chain plumbed yet); see audit.emit for the
    column-level TODO.
    """
    if app_settings.REBAC_REQUIRE_SUDO_REASON and not reason:
        raise SudoReasonRequiredError(
            "sudo() requires a `reason=...` argument when REBAC_REQUIRE_SUDO_REASON is True"
        )
    state = {"reason": reason or ""}
    token = _sudo_state.set(state)
    return token, state, _current_actor.get()


@contextmanager
def _sudo_state_context(*, reason: str | None = None) -> Iterator[None]:
    """Install the ambient bypass state and emit the mandatory audit row."""
    token, state, bypass_actor = _install_sudo_state(reason)
    try:
        from .audit import emit as _emit_audit
        from .models import PermissionAuditEvent

        # `defer_to_commit=False` — sudo blocks may run outside a transaction,
        # and the bypass must always be auditable even if a wrapping transaction
        # later rolls back.
        _emit_audit(
            PermissionAuditEvent.KIND_SUDO_BYPASS,
            actor=bypass_actor,
            origin=bypass_actor,
            reason=state["reason"],
            defer_to_commit=False,
        )
        yield
    finally:
        _sudo_state.reset(token)


@contextmanager
def sudo(*, reason: str | None = None) -> Iterator[None]:
    """Bypass REBAC checks for an explicitly enabled elevated request path.

    `reason` is mandatory unless `REBAC_REQUIRE_SUDO_REASON = False`.

    Async callers should prefer :func:`asudo`, which awaits the audit
    write on the event loop rather than hopping to a worker thread.
    """
    if not app_settings.REBAC_ALLOW_SUDO:
        raise SudoNotAllowedError("sudo() denied: REBAC_ALLOW_SUDO is False")
    with _sudo_state_context(reason=reason):
        yield


@asynccontextmanager
async def _asudo_state_context(*, reason: str | None = None) -> AsyncIterator[None]:
    """Async mirror of :func:`_sudo_state_context`.

    Sets the same ContextVar slot and emits the same audit row, but
    awaits :func:`rebac.audit.aemit` so the INSERT runs on the loop
    instead of through the sync ``Model.objects.create`` /
    worker-thread fallback in :func:`_sudo_state_context`.
    """
    token, state, bypass_actor = _install_sudo_state(reason)
    try:
        from .audit import aemit as _aemit_audit
        from .models import PermissionAuditEvent

        await _aemit_audit(
            PermissionAuditEvent.KIND_SUDO_BYPASS,
            actor=bypass_actor,
            origin=bypass_actor,
            reason=state["reason"],
            defer_to_commit=False,
        )
        yield
    finally:
        _sudo_state.reset(token)


@asynccontextmanager
async def asudo(*, reason: str | None = None) -> AsyncIterator[None]:
    """Async :func:`sudo`. Use in ``async def`` views / middleware / tasks.

    Same semantics as :func:`sudo` — installs the bypass state and
    emits a ``KIND_SUDO_BYPASS`` audit row — but awaits the audit
    write so it never crosses the sync/async boundary.
    """
    if not app_settings.REBAC_ALLOW_SUDO:
        raise SudoNotAllowedError("sudo() denied: REBAC_ALLOW_SUDO is False")
    async with _asudo_state_context(reason=reason):
        yield


@contextmanager
def system_context(*, reason: str | None = None) -> Iterator[None]:
    """Bypass REBAC checks for framework-owned jobs outside a request.

    System tasks such as migrations, asset loaders, and fixture seeders
    still need a fully audited bypass even when deployments disable raw
    request-path `sudo()`.
    """
    with _sudo_state_context(reason=reason):
        yield


@asynccontextmanager
async def asystem_context(*, reason: str | None = None) -> AsyncIterator[None]:
    """Async :func:`system_context`. Same audit guarantees, on the loop."""
    async with _asudo_state_context(reason=reason):
        yield


# ---------- Default resolver ----------


def default_resolver(request: Any) -> SubjectRef | None:
    """Default `request → SubjectRef` resolver. Used by `ActorMiddleware`.

    Override via `REBAC_ACTOR_RESOLVER = "myapp.path.to.resolver"`.

    Resolution outcomes:

    - Authenticated user → ``SubjectRef`` for that user
      (``to_subject_ref(request.user)``).
    - Unauthenticated or missing ``request.user`` → the anonymous SubjectRef
      (``REBAC_ANONYMOUS_TYPE:*``). Schemas with ``permission read = ... +
      anonymous`` (or wildcard relations like ``viewer: auth/anonymous:*``)
      match this subject.
    - Authenticated user that fails ``to_subject_ref`` (e.g. an unregistered
      subject class) → the anonymous SubjectRef, as a fail-safe.

    The return type is ``SubjectRef | None`` so a custom resolver may
    return ``None`` to signal "no resolution attempted" — distinct from
    "anonymous resolved". The default resolver itself never returns
    ``None``; missing / unauthenticated users collapse to the anonymous
    SubjectRef.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return anonymous_actor()
    try:
        return to_subject_ref(user)
    except NoActorResolvedError:
        return anonymous_actor()


def get_actor_resolver() -> Callable[[Any], SubjectRef | None]:
    """Look up the actor resolver from settings."""
    path = app_settings.REBAC_ACTOR_RESOLVER
    module_path, _, attr = path.rpartition(".")
    module = import_module(module_path)
    resolver: Callable[[Any], SubjectRef | None] = getattr(module, attr)
    return resolver


# ---------- Composing custom resolvers ----------


def chain_resolvers(
    *resolvers: Callable[[Any], SubjectRef | None],
    terminal: Callable[[Any], SubjectRef | None] | None = default_resolver,
) -> Callable[[Any], SubjectRef | None]:
    """Compose actor resolvers; first non-``None`` result wins.

    Tries each `resolver` in order and returns the first non-``None``
    :class:`SubjectRef`. If every supplied resolver declines, falls
    through to `terminal` (default: :func:`default_resolver`). Pass
    ``terminal=None`` to disable the fallback — the composed resolver
    then returns ``None`` when every supplied resolver declines, which
    :class:`~rebac.middleware.ActorMiddleware` surfaces as
    :class:`~rebac.errors.NoActorResolvedError`.

    Typical use: a downstream addon stacks alternative credential paths
    (bearer-token → API key, service-token header → service account,
    …) in front of the default user/anonymous resolution::

        from rebac.actors import bearer_token, chain_resolvers


        def _api_key_resolver(request):
            token = bearer_token(request)
            if not token:
                return None
            key = ApiKey.objects.filter(token_hash=hash(token)).first()
            return key.as_subject_ref() if key else None


        resolve = chain_resolvers(_api_key_resolver)

        # In settings.py:
        REBAC_ACTOR_RESOLVER = "myapp.actors.resolve"

    Assign the result to a module-level name and reference it by
    dotted-path from ``REBAC_ACTOR_RESOLVER``; the setting machinery
    resolves the name via ``importlib.import_module``, not via pickle,
    so the composed inner closure never needs to round-trip through
    ``pickle.dumps``.
    """

    chain = tuple(resolvers)

    def chained(request: Any) -> SubjectRef | None:
        for resolver in chain:
            ref = resolver(request)
            if ref is not None:
                return ref
        if terminal is None:
            return None
        return terminal(request)

    return chained


def bearer_token(request: Any) -> str:
    """Extract ``Bearer <token>`` value from an HTTP Authorization header.

    Reads ``request.META["HTTP_AUTHORIZATION"]`` — the standard Django
    :class:`~django.http.HttpRequest` shape, also produced by DRF and
    Strawberry's Django integration. The scheme match is
    case-insensitive per RFC 7235; the returned token is stripped of
    surrounding whitespace.

    Returns an empty string when no Bearer credential is present, so
    callers can short-circuit on falsiness without distinguishing
    "no header" from "wrong scheme" from "empty value"::

        token = bearer_token(request)
        if not token:
            return None
        ...

    Pair with :func:`chain_resolvers` to build credential-aware actor
    resolvers without re-deriving the header parse at every call site.
    """

    meta = getattr(request, "META", None)
    if not isinstance(meta, dict):
        return ""
    header = meta.get("HTTP_AUTHORIZATION", "")
    if not isinstance(header, str):
        return ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()
