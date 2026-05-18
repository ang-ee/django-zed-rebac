"""System checks — registered at app-ready. No DB queries; no model instantiation."""

from __future__ import annotations

import logging
from typing import Any

from django.core import checks
from django.db.utils import DatabaseError

from .conf import app_settings


@checks.register("rebac")
def check_backend_setting(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    issues: list[checks.CheckMessage] = []
    backend = app_settings.REBAC_BACKEND
    if backend not in ("local", "spicedb"):
        issues.append(
            checks.Error(
                f"REBAC_BACKEND={backend!r} (expected 'local' or 'spicedb')",
                id="rebac.E001",
            )
        )
    if backend == "spicedb":
        if not app_settings.REBAC_SPICEDB_ENDPOINT:
            issues.append(
                checks.Error(
                    "REBAC_SPICEDB_ENDPOINT must be set when REBAC_BACKEND='spicedb'",
                    id="rebac.E002",
                )
            )
        if not app_settings.REBAC_SPICEDB_TOKEN:
            issues.append(
                checks.Error(
                    "REBAC_SPICEDB_TOKEN must be set when REBAC_BACKEND='spicedb'",
                    id="rebac.E002",
                )
            )
    return issues


@checks.register("rebac", deploy=True)
def check_production_settings(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    issues: list[checks.CheckMessage] = []
    if app_settings.REBAC_BACKEND == "spicedb" and not app_settings.REBAC_SPICEDB_TLS:
        issues.append(
            checks.Warning(
                "REBAC_SPICEDB_TLS=False in production. Set to True for "
                "non-localhost SpiceDB endpoints.",
                id="rebac.W101",
            )
        )
    return issues


@checks.register("rebac")
def check_auth_backend_installed(
    app_configs: Any = None, **kwargs: Any
) -> list[checks.CheckMessage]:
    """Warn if `rebac.backends.auth.RebacBackend` is not in AUTHENTICATION_BACKENDS."""
    from django.conf import settings

    backends = getattr(settings, "AUTHENTICATION_BACKENDS", [])
    if not any(b.endswith(".RebacBackend") or b.endswith(".auth.RebacBackend") for b in backends):
        return [
            checks.Warning(
                "rebac.backends.auth.RebacBackend not in AUTHENTICATION_BACKENDS. "
                "Per-object `user.has_perm(perm, obj)` checks will not route through REBAC.",
                id="rebac.W001",
                hint=(
                    'Add "rebac.backends.auth.RebacBackend" to '
                    "AUTHENTICATION_BACKENDS, before django.contrib.auth.backends.ModelBackend."
                ),
            )
        ]
    return []


@checks.register("rebac")
def check_actor_middleware_order(
    app_configs: Any = None, **kwargs: Any
) -> list[checks.CheckMessage]:
    """Ensure ActorMiddleware appears after AuthenticationMiddleware."""
    from django.conf import settings

    middleware = list(getattr(settings, "MIDDLEWARE", []))
    actor_path = "rebac.middleware.ActorMiddleware"
    auth_path = "django.contrib.auth.middleware.AuthenticationMiddleware"
    if actor_path not in middleware:
        return []
    if auth_path not in middleware:
        return [
            checks.Error(
                f"{actor_path} requires {auth_path} in MIDDLEWARE.",
                id="rebac.E003",
            )
        ]
    if middleware.index(actor_path) < middleware.index(auth_path):
        return [
            checks.Error(
                f"{actor_path} must appear after {auth_path} in MIDDLEWARE.",
                id="rebac.E004",
            )
        ]
    return []


def _is_rebac_bound(model: Any) -> bool:
    """A model is RBAC-bound iff its ``_meta`` carries a truthy
    ``rebac_resource_type`` attribute (set by ``RebacModelBase`` from
    ``Meta.rebac_resource_type``)."""
    meta = getattr(model, "_meta", None)
    if meta is None:
        return False
    return bool(getattr(meta, "rebac_resource_type", None))


@checks.register("rebac")
def check_universal_admin_in_roles(
    app_configs: Any = None, **kwargs: Any
) -> list[checks.CheckMessage]:
    """Warn when a ``<namespace>/role`` definition is missing the universal-admin
    role's ``#member`` subject in its ``member`` relation type union.

    The expected pattern (per the ``rebac.roles`` convention) is::

        definition storage/role {
            relation member: auth/user
                           | auth/group#member
                           | angee/role:admin#member       // universal admin
        }

    Granting an actor membership in ``angee/role:admin`` then makes them
    a member of *every* role object in every opted-in addon, automatically.

    The role checked is configurable via ``REBAC_UNIVERSAL_ADMIN_ROLE``
    (default ``"angee/role:admin"``). Set to ``None`` to disable the
    check entirely.

    Only fires when the schema has been loaded from the DB (post-``rebac
    sync``); a fresh install with no rows produces no warnings.
    """
    universal = app_settings.REBAC_UNIVERSAL_ADMIN_ROLE
    if not universal:
        return []
    if ":" not in universal:
        return [
            checks.Error(
                f"REBAC_UNIVERSAL_ADMIN_ROLE={universal!r} is not a valid "
                f"<namespace>/role:<name> spec",
                id="rebac.E005",
            )
        ]
    expected_type, expected_id = universal.split(":", 1)

    # Pull the schema via the singleton backend so tests / manual
    # ``set_schema`` calls land in the same instance the check inspects.
    # System checks run in three states where the schema is unloadable
    # and the check must be a no-op rather than aborting startup:
    #   - fresh install before migrations (``DatabaseError``)
    #   - pytest without the ``django_db`` mark (``RuntimeError`` from
    #     the pytest-django access guard)
    #   - any unanticipated env where the singleton backend isn't ready.
    # The broad catch is deliberate, but we log the exception at DEBUG
    # so a real parser bug or backend misconfiguration is still
    # diagnosable rather than fully silenced.
    try:
        # Lazy import — `rebac.backends` triggers schema loading on first
        # touch and module-level import would break app-registry boot
        # order during `python manage.py migrate`.
        from .backends import backend as _backend
        from .backends.base import Backend

        b: Backend = _backend()
        if not hasattr(b, "schema"):
            return []
        schema = b.schema()  # type: ignore[attr-defined]
    except (DatabaseError, RuntimeError) as exc:  # pragma: no cover — install/test paths
        logging.getLogger("rebac.checks").debug(
            "Universal-admin check skipped: schema unavailable (%s)", exc
        )
        return []

    issues: list[checks.CheckMessage] = []
    for definition in schema.definitions:
        # Only role definitions — by convention these live under
        # ``<namespace>/role`` resource types.
        if not definition.resource_type.endswith("/role"):
            continue
        # The universal-admin role itself is exempt (it would otherwise
        # reference itself, creating a self-loop with no semantic).
        if definition.resource_type == expected_type:
            continue
        member_relation = next(
            (r for r in definition.relations if r.name == "member"),
            None,
        )
        if member_relation is None:
            continue  # role definition without a member relation — handled by other lint
        has_universal = any(
            sub.type == expected_type and sub.id == expected_id and sub.relation == "member"
            for sub in member_relation.allowed_subjects
        )
        if not has_universal:
            issues.append(
                checks.Warning(
                    f"Role definition {definition.resource_type!r} is missing "
                    f"the universal-admin entry ({universal}#member) from its "
                    f"member relation type union. Grants of {universal} will not "
                    f"flow through to this role.",
                    hint=(
                        f"Add `| {universal}#member` to the `member` relation's "
                        f"type union in {definition.resource_type}'s rebac.zed "
                        f"definition. Or set REBAC_UNIVERSAL_ADMIN_ROLE = None "
                        f"to disable this check globally."
                    ),
                    id="rebac.W004",
                )
            )
    return issues


@checks.register("rebac")
def check_cross_rbac_relations(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    """Warn when an RBAC-bound model declares a forward FK / O2O / M2M whose
    target is also RBAC-bound.

    Bare-string ``qs.prefetch_related("rel")`` against an RBAC-bound related
    model does not auto-scope in v1 — callers must use the explicit
    ``Prefetch(queryset=Related.objects.with_actor(actor))`` form. This check
    surfaces every such relation at startup so the JOIN-leak surface is
    greppable. Reverse accessors are out of scope for v1.

    Off by default because the warning fires for the *existence* of the
    relation, not for any actual bare-string usage — on a healthy
    codebase that already wraps prefetches in
    ``Prefetch(queryset=...with_actor(actor))`` the check is pure noise
    and drowns real warnings. Opt in by setting
    ``REBAC_LINT_BARE_PREFETCH = True`` when you want the one-off audit.
    """
    if not app_settings.REBAC_LINT_BARE_PREFETCH:
        return []

    from django.apps import apps
    from django.db import models

    issues: list[checks.CheckMessage] = []
    relation_types = (models.ForeignKey, models.OneToOneField, models.ManyToManyField)
    for model in apps.get_models():
        if not _is_rebac_bound(model):
            continue
        for field in model._meta.get_fields():
            # Limit to forward declarations: skip reverse accessors and
            # auto-created intermediates.
            if not isinstance(field, relation_types):
                continue
            related = getattr(field, "related_model", None)
            if related is None:
                continue
            if not _is_rebac_bound(related):
                continue
            model_label = f"{model._meta.app_label}.{model.__name__}"
            related_label = f"{related._meta.app_label}.{related.__name__}"
            issues.append(
                checks.Warning(
                    f"{model_label}.{field.name}: bare-string "
                    "select_related/prefetch_related against RBAC-bound "
                    f"related model {related_label} won't auto-scope.",
                    hint=(
                        f'Use Prefetch("{field.name}", queryset='
                        f"{related.__name__}.objects.with_actor(actor))."
                    ),
                    obj=model,
                    id="rebac.W003",
                )
            )
    return issues
