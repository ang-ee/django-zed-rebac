"""The public import surface documented in CLAUDE.md and ARCHITECTURE.md § Public API surface."""

from __future__ import annotations

import importlib

import pytest
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied

import rebac

# ARCHITECTURE.md § Public API surface.
ARCHITECTURE_SURFACE = {
    "rebac": [
        "RebacMixin",
        "RebacTrackedMixin",
        "RebacManager",
        "RebacQuerySet",
        "TrackedManager",
        "TrackedQuerySet",
        "require_permission",
        "rebac_resource",
        "Backend",
        "LocalBackend",
        "SpiceDBBackend",
        "CheckResult",
        "Consistency",
        "Zookie",
        "ObjectRef",
        "SubjectRef",
        "RelationshipTuple",
        "PermissionDenied",
        "MissingActorError",
        "CaveatUnsupportedError",
        "PermissionDepthExceeded",
        "NoActorResolvedError",
        "RelationshipReadError",
        "ActorLike",
        "current_actor",
        "set_current_actor",
        "actor_context",
        "sudo",
        "system_context",
        "ANONYMOUS_ACTOR",
        "anonymous_actor",
        "is_anonymous_actor",
        "write_relationships",
        "schema_changes",
        "delete_relationships",
        "delete_relationship",
        "backend",
        "check_new",
        "chain_resolvers",
        "bearer_token",
        "app_settings",
    ],
    "rebac.drf": ["RebacPermission", "RebacFilterBackend"],
    "rebac.mcp": ["rebac_mcp_tool", "default_actor_resolver", "get_mcp_actor_resolver"],
    "rebac.schema": ["parse_zed", "validate_schema"],
    "rebac.memberships": ["grant", "revoke", "members_of", "containers_of"],
    "rebac.roles": ["grant", "revoke", "roles_of", "members_of"],
    "rebac.testing": ["install_schema"],
}

# CLAUDE.md § Public API surface.
CLAUDE_SURFACE = {
    "rebac": [
        "RebacMixin",
        "require_permission",
        "rebac_resource",
        "rebac_subject",
        "Backend",
        "LocalBackend",
        "SpiceDBBackend",
        "backend",
        "CheckResult",
        "Consistency",
        "Zookie",
        "PermissionResult",
        "ObjectRef",
        "SubjectRef",
        "RelationshipTuple",
        "ActorLike",
        "PermissionDenied",
        "MissingActorError",
        "CaveatUnsupportedError",
        "PermissionDepthExceeded",
        "NoActorResolvedError",
        "SchemaError",
        "current_actor",
        "set_current_actor",
        "actor_context",
        "sudo",
        "system_context",
        "to_subject_ref",
        "grant_subject_ref",
        "to_object_ref",
        "write_relationships",
        "delete_relationships",
        "app_settings",
    ],
    "rebac.drf": ["RebacPermission", "RebacFilterBackend"],
    "rebac.mcp": ["rebac_mcp_tool"],
}

PUBLIC_ERRORS = [
    "PermissionDenied",
    "MissingActorError",
    "CaveatUnsupportedError",
    "PermissionDepthExceeded",
    "NoActorResolvedError",
    "RelationshipReadError",
    "SchemaError",
]


def _pairs(surface: dict[str, list[str]]) -> list[tuple[str, str]]:
    return [(module, name) for module, names in surface.items() for name in names]


@pytest.mark.parametrize("name", rebac.__all__)
def test_every_exported_name_resolves(name: str) -> None:
    assert getattr(rebac, name) is not None


def test_exports_are_unique() -> None:
    assert len(rebac.__all__) == len(set(rebac.__all__))


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no attribute 'not_public'"):
        _ = rebac.not_public  # type: ignore[attr-defined]


@pytest.mark.parametrize(("module", "name"), _pairs(ARCHITECTURE_SURFACE))
def test_architecture_public_surface_is_importable(module: str, name: str) -> None:
    assert getattr(importlib.import_module(module), name) is not None
    if module == "rebac":
        assert name in rebac.__all__


@pytest.mark.parametrize(("module", "name"), _pairs(CLAUDE_SURFACE))
def test_claude_md_public_surface_is_importable(module: str, name: str) -> None:
    assert getattr(importlib.import_module(module), name) is not None
    if module == "rebac":
        assert name in rebac.__all__


def test_celery_adapter_is_not_shipped() -> None:
    """ARCHITECTURE.md § Celery: automatic propagation is planned, not shipped."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("rebac.celery")


@pytest.mark.parametrize("name", PUBLIC_ERRORS)
def test_public_errors_share_the_rebac_base(name: str) -> None:
    error = getattr(rebac, name)
    assert issubclass(error, rebac.RebacError)
    assert issubclass(error, Exception)


def test_permission_denied_is_djangos_permission_denied() -> None:
    assert issubclass(rebac.PermissionDenied, DjangoPermissionDenied)
    with pytest.raises(DjangoPermissionDenied):
        raise rebac.PermissionDenied("denied")


def test_sudo_errors_share_the_rebac_base() -> None:
    from rebac.errors import SudoNotAllowedError, SudoReasonRequiredError

    assert issubclass(SudoNotAllowedError, rebac.RebacError)
    assert issubclass(SudoReasonRequiredError, rebac.RebacError)
