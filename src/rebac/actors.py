"""Actor resolution: ContextVar, Subject conversion, sudo() / system_context().

The ContextVar `_current_actor` is the *ambient* actor (populated by middleware
and Celery prerun hooks). It is read-only at call sites — `set_current_actor`
should only be invoked at framework boundaries.

Per-queryset actors take strict priority over the ContextVar. See
ARCHITECTURE.md § Three actor-resolution paths.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

from django.contrib.auth import get_user_model

from .conf import app_settings
from .errors import (
    NoActorResolvedError,
    SudoNotAllowedError,
    SudoReasonRequiredError,
)
from .types import ObjectRef, SubjectRef

if TYPE_CHECKING:
    from .backends.base import Backend
    from .types import Consistency

# ---------- ContextVar ----------

_current_actor: ContextVar[SubjectRef | None] = ContextVar("rebac_current_actor", default=None)
_sudo_state: ContextVar[dict[str, Any] | None] = ContextVar("rebac_sudo", default=None)


# ---------- Legacy cache helpers (deprecated in 0.4, removed in 0.6) ----------
#
# Proposal 0002 unified per-request caching under
# :class:`rebac.evaluator.PermissionEvaluator`. The three helpers below —
# ``accessible_cached``, ``enable_accessible_cache``,
# ``disable_accessible_cache`` — keep their public signatures so existing
# middleware / custom request hooks keep working, but each delegates to
# the evaluator and emits a single-shot ``DeprecationWarning`` so
# downstream callers see the warning at most once per process.
#
# Removal in 0.6 alongside the denormalized storage path (proposal 0001).

_LEGACY_WARNED: set[str] = set()


def _warn_legacy_once(name: str, message: str) -> None:
    """Emit a DeprecationWarning at most once per process per call site."""
    if name in _LEGACY_WARNED:
        return
    _LEGACY_WARNED.add(name)
    warnings.warn(message, DeprecationWarning, stacklevel=3)


def accessible_cached(
    backend: Backend,
    *,
    subject: SubjectRef,
    action: str,
    resource_type: str,
    context: dict | None = None,
    consistency: Consistency | None = None,
) -> tuple[str, ...]:
    """Deprecated alias for ``current_evaluator().accessible(...)``.

    Kept for 0.4 backward compat; removed in 0.6. New code should call
    the evaluator directly (or just call ``backend.accessible(...)`` —
    the evaluator is consulted automatically by the middleware-bracketed
    request path).
    """
    _warn_legacy_once(
        "accessible_cached",
        "rebac.actors.accessible_cached is deprecated; use "
        "rebac.evaluator.current_evaluator().accessible(...) instead. "
        "Will be removed in v0.6.",
    )
    from .evaluator import PermissionEvaluator, current_evaluator

    evaluator = current_evaluator() or PermissionEvaluator()
    return evaluator.accessible(
        backend,
        subject=subject,
        action=action,
        resource_type=resource_type,
        context=context,
        consistency=consistency,
    )


def enable_accessible_cache() -> Any:
    """Deprecated alias for opening a :func:`rebac.evaluator.evaluator_scope`.

    Returns an opaque token compatible with the historic
    ``disable_accessible_cache`` teardown. Existing middleware that
    brackets a request with these two calls keeps working; new code
    should use ``with evaluator_scope(): ...`` instead.
    """
    _warn_legacy_once(
        "enable_accessible_cache",
        "rebac.actors.enable_accessible_cache is deprecated; use "
        "`with rebac.evaluator.evaluator_scope(): ...` instead. "
        "Will be removed in v0.6.",
    )
    from .evaluator import PermissionEvaluator, _current_evaluator

    return _current_evaluator.set(
        PermissionEvaluator(max_size=app_settings.REBAC_EVALUATOR_CACHE_SIZE)
    )


def disable_accessible_cache(token: Any) -> None:
    """Deprecated alias for the corresponding ``evaluator_scope`` teardown."""
    _warn_legacy_once(
        "disable_accessible_cache",
        "rebac.actors.disable_accessible_cache is deprecated; use "
        "`with rebac.evaluator.evaluator_scope(): ...` instead. "
        "Will be removed in v0.6.",
    )
    from .evaluator import _current_evaluator

    _current_evaluator.reset(token)


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
    return SubjectRef.of(app_settings.REBAC_ANONYMOUS_TYPE, "*")


# Module-level convenience constant. Uses the default ``REBAC_ANONYMOUS_TYPE``
# at import time; consumers that override the setting should call
# :func:`anonymous_actor` instead.
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
        subject.subject_type == app_settings.REBAC_ANONYMOUS_TYPE
        and subject.subject_id == "*"
        and not subject.optional_relation
    )


def is_sudo() -> bool:
    return _sudo_state.get() is not None


def current_sudo_reason() -> str | None:
    state = _sudo_state.get()
    return None if state is None else state.get("reason")


# ---------- Subject conversion ----------


class _RebacSubjectMarker(Protocol):
    """Anything decorated with @rebac_subject exposes _rebac_type and _rebac_id_attr."""

    _rebac_type: str
    _rebac_id_attr: str


ActorLike = SubjectRef | _RebacSubjectMarker | Any
"""Anything resolvable to a `SubjectRef`.

Concretely accepts:
  - `SubjectRef` (passed through)
  - Django `User` instance (→ `auth/user:<pk>`)
  - Django `Group` instance (→ `auth/group:<pk>#member`)
  - Any class decorated with `@rebac_subject(...)` (→ `<type>:<id_attr_value>`)
"""


_subject_registry: dict[type, tuple[str, str]] = {}
"""Mapping `cls -> (rebac_type, id_attr)` populated by `@rebac_subject`."""


def rebac_subject(*, type: str, id_attr: str = "pk") -> Callable[[type], type]:
    """Decorator: register a class as a subject type.

    Example:
        @rebac_subject(type="auth/apikey", id_attr="public_id")
        class ApiKey: ...
    """

    def _decorator(cls: type) -> type:
        _subject_registry[cls] = (type, id_attr)
        cls._rebac_type = type  # type: ignore[attr-defined]
        cls._rebac_id_attr = id_attr  # type: ignore[attr-defined]
        return cls

    return _decorator


def to_subject_ref(actor: ActorLike) -> SubjectRef:
    """Resolve `actor` to a `SubjectRef`. Raises `NoActorResolvedError`.

    Django's ``AnonymousUser`` resolves to the anonymous SubjectRef
    (``REBAC_ANONYMOUS_TYPE:*``) — see :func:`is_anonymous_actor`. Passing
    raw ``None`` is a framework error (the resolver chain failed) and still
    raises.
    """
    if actor is None:
        raise NoActorResolvedError("Cannot resolve None to a SubjectRef")
    if isinstance(actor, SubjectRef):
        return actor

    # AnonymousUser inherits from ``object``, not the swappable user model,
    # so it bypasses the isinstance(user_model) check below. Handle it
    # explicitly first — anonymous reads are a first-class authorization
    # path, not an error.
    try:
        from django.contrib.auth.models import AnonymousUser
    except ImportError:  # pragma: no cover
        AnonymousUser = None  # type: ignore[assignment]
    if AnonymousUser is not None and isinstance(actor, AnonymousUser):
        return anonymous_actor()

    from ._id import subject_id_attr

    user_model = get_user_model()
    if isinstance(actor, user_model):
        # Strict-by-default (CLAUDE.md § 3): a user-model instance with
        # ``is_authenticated == False`` is almost always a programming bug
        # (forgot to save, deleted user re-used, mid-test fixture). Anonymous
        # has a dedicated singleton (``AnonymousUser``) — silently downgrading
        # to it would mask the bug class the strict posture is meant to
        # surface. The request-path resolver (:func:`default_resolver`)
        # remains the fail-safe via its ``except NoActorResolvedError`` path.
        if not getattr(actor, "is_authenticated", False):
            raise NoActorResolvedError(
                f"{type(actor).__name__} instance has is_authenticated=False. "
                "Pass AnonymousUser explicitly for the anonymous actor, or "
                "save/load a real user row."
            )
        attr = subject_id_attr(user_model)
        return SubjectRef.of(app_settings.REBAC_USER_TYPE, str(getattr(actor, attr)))

    # Group?
    try:
        from django.contrib.auth.models import Group
    except ImportError:  # pragma: no cover
        Group = None  # type: ignore[assignment]
    if Group is not None and isinstance(actor, Group):
        attr = subject_id_attr(Group)
        return SubjectRef.of(
            app_settings.REBAC_GROUP_TYPE,
            str(getattr(actor, attr)),
            "member",
        )

    # @rebac_subject-registered?
    for cls, (type_, id_attr) in _subject_registry.items():
        if isinstance(actor, cls):
            value = getattr(actor, id_attr)
            return SubjectRef.of(type_, str(value))

    raise NoActorResolvedError(
        f"Cannot resolve {type(actor).__name__} instance to SubjectRef. "
        f"Decorate the class with @rebac_subject(type=..., id_attr=...) "
        f"or pass a SubjectRef directly."
    )


def grant_subject_ref(agent: Any, on_behalf_of: Any | None) -> SubjectRef:
    """Build a Grant subject for `agent` acting on behalf of `on_behalf_of`.

    The resolution requires the consumer to have registered both an `agents/agent`
    and `agents/grant` subject type via `@rebac_subject`. The plugin itself doesn't
    ship those types — they live in the consumer's `agents` app. We synthesise
    a deterministic grant id from the (agent_id, user_id) pair.

    For systems where grants live in the DB, override
    `REBAC_ACTOR_RESOLVER` to translate (request, agent, user) into the
    persisted grant id.
    """
    agent_ref = to_subject_ref(agent)
    if on_behalf_of is None:
        # Standalone agent run — no user-grant intersection.
        return agent_ref
    user_ref = to_subject_ref(on_behalf_of)
    grant_id = f"{user_ref.subject_id}.{agent_ref.subject_id}"
    return SubjectRef(
        object=ObjectRef("agents/grant", grant_id),
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


@contextmanager
def _sudo_state_context(*, reason: str | None = None) -> Iterator[None]:
    """Install the ambient bypass state and emit the mandatory audit row."""
    if app_settings.REBAC_REQUIRE_SUDO_REASON and not reason:
        raise SudoReasonRequiredError(
            "sudo() requires a `reason=...` argument when REBAC_REQUIRE_SUDO_REASON is True"
        )
    state = {"reason": reason or ""}
    token = _sudo_state.set(state)
    # Capture the ambient actor at sudo entry — that's the subject the bypass
    # is being applied "as". For v1 origin == actor (no impersonation chain
    # plumbed yet); see audit.emit for the column-level TODO.
    bypass_actor = _current_actor.get()
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
    """
    if not app_settings.REBAC_ALLOW_SUDO:
        raise SudoNotAllowedError("sudo() denied: REBAC_ALLOW_SUDO is False")
    with _sudo_state_context(reason=reason):
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

    The return type stays ``SubjectRef | None`` for backwards compatibility
    with custom resolvers that explicitly return ``None`` to signal
    "no resolution attempted"; the default resolver itself never returns
    ``None`` in v0.3+.
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
    return getattr(module, attr)


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

    The composed callable is plain and pickle-safe (no closure captures
    beyond the resolver tuple and the terminal), so it can be assigned
    to a module-level name and referenced from settings directly.
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
