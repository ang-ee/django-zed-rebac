"""Public decorators."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from inspect import signature
from typing import Any

from .actors import current_actor, is_sudo
from .errors import NoActorResolvedError, PermissionDenied
from .resources import rebac_resource as _rebac_resource_register
from .resources import to_object_ref
from .types import ObjectRef, SubjectRef

rebac_resource = _rebac_resource_register


def require_permission(
    action: str,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    resource_arg: str | None = None,
    actor_arg: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Gate a callable on a REBAC permission.

    Usage:
        @require_permission("invoke",
            resource_type="celery/task/reindex",
            resource_id="*")
        @shared_task
        def reindex(): ...

    Or, for callables that receive an instance:
        @require_permission("write", resource_arg="post")
        def edit(post): ...

    The actor resolves from `current_actor()` unless `actor_arg` names a
    declared callable parameter. Named actor and resource parameters may be
    passed positionally or by keyword. An explicit actor always takes
    precedence over ambient sudo.
    """

    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn_signature = signature(fn)
        for option, parameter in (("actor_arg", actor_arg), ("resource_arg", resource_arg)):
            if parameter is not None and parameter not in fn_signature.parameters:
                raise ValueError(
                    f"@require_permission {option}={parameter!r} is not a parameter of "
                    f"{fn.__qualname__}"
                )

        @wraps(fn)
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            from . import backend

            bound = fn_signature.bind(*args, **kwargs)
            bound.apply_defaults()

            # Resolve actor.
            actor_ref: SubjectRef | None
            if actor_arg is not None:
                from .actors import to_subject_ref

                actor = bound.arguments[actor_arg]
                if actor is None:
                    raise NoActorResolvedError(
                        f"@require_permission({action!r}) actor_arg {actor_arg!r} resolved to None"
                    )
                actor_ref = to_subject_ref(actor)
            else:
                if is_sudo():
                    return fn(*args, **kwargs)
                actor_ref = current_actor()
            if actor_ref is None:
                raise NoActorResolvedError(
                    f"@require_permission({action!r}) called with no actor in scope"
                )

            # Resolve resource.
            if resource_arg is not None:
                obj = bound.arguments[resource_arg]
                if obj is None:
                    raise ValueError(f"resource_arg {resource_arg!r} produced no value")
                resource = to_object_ref(obj)
            elif resource_type is not None:
                resource = ObjectRef(resource_type, resource_id or "")
            else:
                raise ValueError(
                    "@require_permission requires either resource_type=... or resource_arg=..."
                )

            result = backend().check_access(subject=actor_ref, action=action, resource=resource)
            if not result.allowed:
                raise PermissionDenied(f"Denied: {actor_ref} cannot {action} {resource}")
            return fn(*args, **kwargs)

        return _wrapped

    return _decorator
