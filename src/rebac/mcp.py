"""MCP / FastMCP integration: the ``rebac_mcp_tool`` decorator.

Implements `proposal 0004 <docs/proposals/0004-mcp-tool-integration.md>`. The
decorator turns an MCP tool call into a permission check: it resolves the
*already-authenticated* actor, checks the target permission, and only then runs
the tool body.

Authentication is **not** this decorator's job. Minting and validating identity
remains the MCP server / transport layer's responsibility (CLAUDE.md
§ "Not an authentication system"). The decorator only *resolves* an actor that
some upstream boundary already established, then authorises it.

Actor resolution order (first hit wins):

1. The per-call MCP request context: the configurable resolver named by
   ``REBAC_MCP_ACTOR_RESOLVER`` (default :func:`default_actor_resolver`, which
   reads ``ctx.request_context.meta["actor_subject"]`` as a canonical
   :class:`~rebac.SubjectRef` string such as ``auth/user:42`` or
   ``agents/grant:42.assistant#valid``).
2. The ambient :func:`rebac.current_actor` — a transport middleware may have
   populated it at the request boundary.

The per-call ctx actor is the explicit identity the transport established for
*this* request, so it outranks the ambient ContextVar — matching the project
rule that explicit local scope beats ambient context (CLAUDE.md § 5) and
avoiding privilege confusion from a leaked ambient actor.

No actor resolved → fail closed (:class:`rebac.PermissionDenied`).

SDK neutrality
--------------
This module targets the FastMCP ``Context`` shape
(``ctx.request_context.meta``) but never imports the ``mcp`` SDK: the context
is read through the small :func:`_context_meta` accessor by duck-typing. That
keeps the module import-light (it loads with or without the SDK installed) and
isolates the one SDK-shaped assumption to a single function, so swapping in the
official SDK's context shape later is a one-function change rather than a
rewrite. See proposal 0004 § Design.
"""

from __future__ import annotations

import functools
import inspect
import warnings
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, TypeVar, cast

from asgiref.sync import sync_to_async
from django.utils.module_loading import import_string

from .actors import actor_context, current_actor
from .backends import backend
from .conf import app_settings
from .errors import PermissionDenied
from .preflight import check_new
from .types import CheckResult, ObjectRef, PermissionResult, SubjectRef

_F = TypeVar("_F", bound=Callable[..., Any])

# The singleton resource id for tools that gate on the tool/capability itself
# rather than a per-call target row (``mcp/tool/edit_post:*``).
_SINGLETON_ID = "*"

# Actions that authorise a not-yet-persisted resource go through the preflight
# walker (``check_new``) rather than ``has_access`` on a concrete row. ``create``
# is the canonical create-shaped action; the set is a tuple so the contract is
# greppable and easy to extend without touching the decorator body.
_CREATE_ACTIONS: tuple[str, ...] = ("create",)


def _context_meta(ctx: Any) -> dict[str, Any]:
    """Read ``ctx.request_context.meta`` as a mapping, SDK-neutrally.

    This is the *only* place the FastMCP context shape is assumed. The official
    MCP SDK exposes the same request-scoped metadata under a comparable path;
    when that integration lands, widen the access here rather than at every
    call site. Returns an empty dict when the context carries no usable
    metadata so resolvers can decline cleanly (return ``None``) instead of
    raising on a missing attribute.
    """
    request_context = getattr(ctx, "request_context", None)
    meta = getattr(request_context, "meta", None)
    if isinstance(meta, dict):
        return meta
    # Some context shapes expose ``meta`` as a Pydantic model / namespace; fall
    # back to ``model_dump`` / ``__dict__`` so duck-typed resolvers still work.
    if meta is not None:
        dump = getattr(meta, "model_dump", None)
        if callable(dump):
            dumped = dump()
            if isinstance(dumped, dict):
                return cast(dict[str, Any], dumped)
        as_dict = getattr(meta, "__dict__", None)
        if isinstance(as_dict, dict):
            # Copy: callers must not be able to mutate the request-scoped object
            # through the returned mapping.
            return dict(as_dict)
    return {}


def default_actor_resolver(ctx: Any) -> SubjectRef | None:
    """Resolve the actor from ``ctx.request_context.meta["actor_subject"]``.

    ``actor_subject`` is a canonical :class:`~rebac.SubjectRef` string —
    ``auth/user:42``, ``agents/grant:42.assistant#valid``, ``auth/apikey:k_1``.
    Returns ``None`` when no such key is present *or* when the value is not a
    parseable ref, so the decorator falls through to its fail-closed deny — a
    missing or malformed actor is a clean deny, never a 500.
    """
    raw = _context_meta(ctx).get("actor_subject")
    if not raw or not isinstance(raw, str):
        return None
    try:
        return SubjectRef.parse(raw)
    except ValueError:
        # Malformed request metadata — decline (fail closed) rather than letting
        # the parse error escape the tool wrapper as a server error.
        return None


def get_mcp_actor_resolver() -> Callable[[Any], SubjectRef | None]:
    """Look up the MCP actor resolver from ``REBAC_MCP_ACTOR_RESOLVER``.

    Mirrors :func:`rebac.actors.get_actor_resolver`. The dotted path is resolved
    on every call (via Django's :func:`~django.utils.module_loading.import_string`)
    so a ``setting_changed`` override (test ergonomics) takes effect immediately;
    a malformed (e.g. dotless) path raises a clear ``ImportError`` rather than a
    cryptic empty-module-name error.
    """
    resolver: Callable[[Any], SubjectRef | None] = import_string(
        app_settings.REBAC_MCP_ACTOR_RESOLVER
    )
    return resolver


def _resolve_actor(ctx: Any) -> SubjectRef | None:
    """Per-call ctx actor first, then ambient :func:`current_actor`. ``None`` = deny.

    The ctx-supplied actor is the explicit per-request identity, so it outranks
    the ambient ContextVar (CLAUDE.md § 5). Ambient is only the fallback when the
    transport did not stamp an actor on this call's context.
    """
    if ctx is not None:
        actor = get_mcp_actor_resolver()(ctx)
        if actor is not None:
            return actor
    return current_actor()


def _find_context(bound: inspect.BoundArguments) -> Any:
    """Locate the MCP ``Context`` among the call's bound arguments.

    FastMCP injects the context as a keyword (conventionally ``ctx``) whose
    declared default is ``CurrentContext()``. We locate it by the conventional
    ``ctx`` / ``context`` parameter name first, then fall back to any argument
    whose type name is ``Context`` *and* that carries a ``request_context``
    attribute — the shape we actually read. Requiring the attribute keeps an
    unrelated argument of some other class named ``Context`` from being mistaken
    for the MCP context, without importing the SDK to ``isinstance``-check it.
    """
    args = bound.arguments
    for name in ("ctx", "context"):
        if name in args and args[name] is not None:
            return args[name]
    for value in args.values():
        if (
            value is not None
            and type(value).__name__ == "Context"
            and hasattr(value, "request_context")
        ):
            return value
    return None


def _object_ref(
    *,
    resource_type: str,
    id_arg: str | None,
    resource_id: str | None,
    bound: inspect.BoundArguments,
) -> ObjectRef:
    """Build the target ``ObjectRef``: ``id_arg`` kwarg, then ``resource_id=``,
    then the singleton ``"*"``.

    A present-but-``None`` ``id_arg`` (a tool parameter with a default the caller
    omitted) is treated as "not supplied" — it falls through to ``resource_id`` /
    the singleton rather than checking a bogus ``<type>:None`` row.
    """
    if id_arg is not None:
        value = bound.arguments.get(id_arg)
        if value is not None:
            return ObjectRef(resource_type, str(value))
    if resource_id is not None:
        return ObjectRef(resource_type, resource_id)
    return ObjectRef(resource_type, _SINGLETON_ID)


def _create_overlay(
    *,
    create_relations: Mapping[str, str],
    bound: inspect.BoundArguments,
) -> dict[str, Sequence[SubjectRef]]:
    """Build the :func:`check_new` relationship overlay from the bound call args.

    ``create_relations`` maps each relation name the not-yet-persisted row would
    carry to the call argument holding the subject it would point at, as a
    canonical ref string (e.g. ``"blog/vault:v1"``). A relation whose argument is
    absent, ``None``, or not a parseable ref contributes no candidate — so that
    path resolves empty and the create check fails closed for it.
    """
    overlay: dict[str, Sequence[SubjectRef]] = {}
    for relation, arg_name in create_relations.items():
        value = bound.arguments.get(arg_name)
        if value is None:
            continue
        try:
            overlay[relation] = [SubjectRef.parse(str(value))]
        except ValueError:
            continue
    return overlay


def _deny_message(
    *,
    actor: SubjectRef,
    action: str,
    target: ObjectRef | str,
    result: CheckResult,
) -> str:
    """Render the ``PermissionDenied`` detail, surfacing missing caveat context.

    A ``CONDITIONAL_PERMISSION`` collapses to a deny (fail-closed) but names the
    caveat parameters the call did not supply, so a caller that *could* proceed
    by passing context isn't left with an opaque message (CLAUDE.md § 4).
    """
    detail = ""
    if result.result is PermissionResult.CONDITIONAL_PERMISSION and result.conditional_on:
        detail = f" (missing caveat context: {', '.join(result.conditional_on)})"
    return f"Denied: {actor} cannot {action} {target}{detail}"


def _authorize(
    *,
    func: Callable[..., Any],
    resource_type: str,
    action: str,
    id_arg: str | None,
    resource_id: str | None,
    create_relations: Mapping[str, str] | None,
    signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> SubjectRef:
    """Resolve the actor and run the permission check. Raises
    :class:`PermissionDenied` on no-actor or deny; returns the actor on allow.

    Shared by the sync and async wrappers so the authorization contract lives
    in exactly one place.
    """
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()

    actor = _resolve_actor(_find_context(bound))
    if actor is None:
        raise PermissionDenied(
            f"{func.__name__}: no actor resolved for MCP tool call "
            f"(set request meta 'actor_subject' or populate current_actor())."
        )

    target: ObjectRef | str
    if action in _CREATE_ACTIONS:
        overlay = (
            _create_overlay(create_relations=create_relations, bound=bound)
            if create_relations
            else None
        )
        result = check_new(
            subject=actor,
            action=action,
            resource_type=resource_type,
            relationships=overlay,
        )
        target = resource_type
    else:
        resource = _object_ref(
            resource_type=resource_type,
            id_arg=id_arg,
            resource_id=resource_id,
            bound=bound,
        )
        result = backend().check_access(subject=actor, action=action, resource=resource)
        target = resource

    if not result.allowed:
        raise PermissionDenied(
            _deny_message(actor=actor, action=action, target=target, result=result)
        )
    return actor


def rebac_mcp_tool(
    *,
    resource_type: str,
    action: str,
    id_arg: str | None = None,
    resource_id: str | None = None,
    create_relations: Mapping[str, str] | None = None,
    hide_id_arg: bool = False,
) -> Callable[[_F], _F]:
    """Gate an MCP tool body behind a REBAC permission check.

    Wrap the tool function *below* the SDK's ``@mcp.tool`` so this decorator
    runs first on every call::

        @mcp.tool
        @rebac_mcp_tool(resource_type="blog/post", action="write", id_arg="post_id")
        async def edit_post(post_id: str, body: str, ctx: Context = CurrentContext()) -> dict:
            ...

    Arguments:
        resource_type: the REBAC resource type (``"blog/post"``,
            ``"mcp/capability"``).
        action: the permission to check (``"write"``, ``"use"``, ``"create"``).
            ``"create"`` (and any action in :data:`_CREATE_ACTIONS`) routes
            through :func:`rebac.check_new` for not-yet-persisted resources;
            every other action checks :meth:`Backend.check_access` on a concrete
            :class:`ObjectRef`.
        id_arg: name of the tool call argument carrying the target resource id.
        resource_id: a fixed resource id, used when no ``id_arg`` is supplied.
        create_relations: for create-shaped actions, a mapping of relation name →
            the tool call argument holding the subject the new row would point at
            via that relation, as a canonical ref string (``"blog/vault:v1"``).
            Used to build the :func:`check_new` overlay so a relation/arrow-based
            create permission (e.g. ``create = vault->write``) can authorise. A
            create action with no ``create_relations`` can only pass permissions
            built from built-in actor terms (``authenticated`` / ``anonymous``);
            any relation-dependent path fails closed.
        hide_id_arg: best-effort request to drop ``id_arg`` from the published
            MCP input schema (capability-style hidden args). See the note below.

    The resource id resolves as: the ``id_arg`` call argument, else
    ``resource_id=``, else the singleton ``"*"``.

    The wrapped function's signature is preserved unchanged — FastMCP
    introspects ``__signature__`` to build the tool's input schema, so the
    decorator adds and removes no parameters. On allow, the body runs inside
    :func:`rebac.actor_context` so any queryset it builds scopes to the same
    actor without re-resolving it. Sync functions, coroutine functions, and
    async generators (streaming tools) are all supported.

    A ``CONDITIONAL_PERMISSION`` result (caveat context not yet supplied) is
    fail-closed: the body does not run, but the raised :class:`PermissionDenied`
    names the missing caveat parameters so the caller can retry with context.

    ``hide_id_arg``: FastMCP 1.27 builds the tool input schema directly from the
    function signature and exposes no public hook to filter individual
    parameters out of it, so this flag is a no-op against that SDK and emits a
    warning when set. Keep the argument keyword-only (``*, _capability: str =
    ...``) so it stays out of the model-facing positional surface, or default
    it. The flag is honoured only if/when the selected SDK exposes a
    schema-filtering hook (proposal 0004 § Tests). Do not reach into SDK
    internals to force it.
    """
    if hide_id_arg:
        warnings.warn(
            "rebac_mcp_tool(hide_id_arg=True) is a no-op against FastMCP 1.27, "
            "which exposes no public schema-filtering hook. Keep the hidden id "
            "argument keyword-only so it stays off the model-facing surface. "
            "See proposal 0004 § Design.",
            stacklevel=2,
        )

    def decorator(func: _F) -> _F:
        signature = inspect.signature(func)

        def authorize(call_args: tuple[Any, ...], call_kwargs: dict[str, Any]) -> SubjectRef:
            # Single argument-binding site shared by all three wrappers below.
            return _authorize(
                func=func,
                resource_type=resource_type,
                action=action,
                id_arg=id_arg,
                resource_id=resource_id,
                create_relations=create_relations,
                signature=signature,
                args=call_args,
                kwargs=call_kwargs,
            )

        if inspect.isasyncgenfunction(func):

            @functools.wraps(func)
            async def asyncgen_wrapper(*args: Any, **kwargs: Any) -> Any:
                # Authorize off the event loop (the check may touch the ORM), then
                # iterate the streaming body inside the actor context so each
                # yielded chunk is produced under the resolved actor.
                actor = await sync_to_async(authorize, thread_sensitive=True)(args, kwargs)
                with actor_context(actor):
                    async for item in cast(Callable[..., Any], func)(*args, **kwargs):
                        yield item

            return cast(_F, asyncgen_wrapper)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                # The permission check is synchronous and may touch the ORM
                # (LocalBackend). Run it off the event loop via ``sync_to_async``
                # — same posture as ``rebac.audit.aemit`` — then re-enter the
                # actor context on the loop so the awaited body inherits it.
                actor = await sync_to_async(authorize, thread_sensitive=True)(args, kwargs)
                with actor_context(actor):
                    return await cast(Callable[..., Awaitable[Any]], func)(*args, **kwargs)

            return cast(_F, async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            actor = authorize(args, kwargs)
            with actor_context(actor):
                return func(*args, **kwargs)

        return cast(_F, sync_wrapper)

    return decorator


__all__ = [
    "default_actor_resolver",
    "get_mcp_actor_resolver",
    "rebac_mcp_tool",
]
