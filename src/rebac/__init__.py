"""django-zed-rebac — drop-in REBAC engine for Django.

See docs/ARCHITECTURE.md for the design contract. The Python module is `rebac`
(per the naming question in CLAUDE.md, choosing the lean `rebac` form
that matches Django convention: hyphens → underscores).

Note: `RebacMixin` is exposed via `__getattr__` so that this package can be
imported during Django app loading without tripping the model metaclass before
the apps registry is ready. Use `from rebac import RebacMixin` from
*application code* (after Django setup); inside another package's models.py
that's the supported import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.3.0"

default_app_config = "rebac.apps.RebacConfig"

from .actors import (
    ANONYMOUS_ACTOR,
    ActorLike,
    actor_context,
    anonymous_actor,
    bearer_token,
    chain_resolvers,
    current_actor,
    grant_subject_ref,
    is_anonymous_actor,
    rebac_subject,
    set_current_actor,
    sudo,
    system_context,
    to_subject_ref,
)
from .conf import app_settings
from .consistency import (
    current_zookie,
    effective_consistency,
    record_zookie,
    zookie_scope,
)
from .errors import (
    CaveatUnsupportedError,
    MissingActorError,
    NoActorResolvedError,
    PermissionDenied,
    PermissionDepthExceeded,
    RebacError,
    SchemaError,
)
from .evaluator import PermissionEvaluator, current_evaluator, evaluator_scope
from .types import (
    CheckResult,
    Consistency,
    ObjectRef,
    PermissionResult,
    RelationshipTuple,
    SubjectRef,
    Zookie,
)

if TYPE_CHECKING:
    from .audit import emit as emit_audit_event
    from .backends import Backend, LocalBackend, SpiceDBBackend
    from .decorators import rebac_resource, require_permission
    from .mixins import RebacMixin, RebacObjectMeta
    from .permissions_mixin import RebacPermissionsMixin
    from .relationships import delete_relationship, delete_relationships, write_relationships
    from .resources import to_object_ref


_LAZY = {
    "RebacMixin": ("rebac.mixins", "RebacMixin"),
    "RebacObjectMeta": ("rebac.mixins", "RebacObjectMeta"),
    "RebacPermissionsMixin": (
        "rebac.permissions_mixin",
        "RebacPermissionsMixin",
    ),
    "Backend": ("rebac.backends", "Backend"),
    "LocalBackend": ("rebac.backends", "LocalBackend"),
    "SpiceDBBackend": ("rebac.backends", "SpiceDBBackend"),
    "backend": ("rebac.backends", "backend"),
    "require_permission": ("rebac.decorators", "require_permission"),
    "rebac_resource": ("rebac.decorators", "rebac_resource"),
    "write_relationships": ("rebac.relationships", "write_relationships"),
    "delete_relationship": ("rebac.relationships", "delete_relationship"),
    "delete_relationships": ("rebac.relationships", "delete_relationships"),
    "to_object_ref": ("rebac.resources", "to_object_ref"),
    "emit_audit_event": ("rebac.audit", "emit"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'rebac' has no attribute {name!r}")
    from importlib import import_module

    module = import_module(target[0])
    return getattr(module, target[1])


__all__ = [
    "__version__",
    # types
    "ObjectRef",
    "SubjectRef",
    "CheckResult",
    "PermissionResult",
    "Consistency",
    "Zookie",
    "RelationshipTuple",
    # mixin / managers / metaclasses
    "RebacMixin",
    "RebacObjectMeta",
    "RebacPermissionsMixin",
    # decorators
    "require_permission",
    "rebac_resource",
    "rebac_subject",
    # backends
    "Backend",
    "LocalBackend",
    "SpiceDBBackend",
    "backend",
    # actors
    "ActorLike",
    "current_actor",
    "set_current_actor",
    "actor_context",
    "sudo",
    "system_context",
    "to_subject_ref",
    "grant_subject_ref",
    "to_object_ref",
    # composable resolvers
    "bearer_token",
    "chain_resolvers",
    # anonymous
    "ANONYMOUS_ACTOR",
    "anonymous_actor",
    "is_anonymous_actor",
    # errors
    "PermissionDenied",
    "MissingActorError",
    "CaveatUnsupportedError",
    "PermissionDepthExceeded",
    "NoActorResolvedError",
    "SchemaError",
    "RebacError",
    # helpers
    "write_relationships",
    "delete_relationship",
    "delete_relationships",
    "emit_audit_event",
    # evaluator (proposal 0002)
    "PermissionEvaluator",
    "current_evaluator",
    "evaluator_scope",
    # consistency (proposal 0002)
    "current_zookie",
    "record_zookie",
    "zookie_scope",
    "effective_consistency",
    # settings
    "app_settings",
]
