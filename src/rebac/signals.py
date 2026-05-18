"""Pre-save / pre-delete signal handlers gating writes through REBAC."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from ._id import resource_id_attr
from .actors import current_actor as _current_actor
from .actors import is_sudo as _is_sudo
from .conf import app_settings
from .errors import MissingActorError, PermissionDenied
from .mixins import RebacMixin
from .schema.ast import Schema
from .types import ObjectRef, SubjectRef


def _maybe_audit_denial(*, actor: SubjectRef | None, action: str, resource: ObjectRef) -> None:
    """Emit a denial audit row when REBAC_AUDIT_DENIALS is enabled.

    Uses ``defer_to_commit=False`` so the row persists even though the
    raising save / delete is about to roll back the surrounding transaction.

    Audit kind reuses the relevant grant / revoke kind (a denied write is a
    grant that didn't happen; a denied delete is a revoke that didn't
    happen). The reason text carries the ``denied:`` prefix so consumers
    can distinguish denial from successful writes when querying the trail.
    """
    if not app_settings.REBAC_AUDIT_DENIALS:
        return
    from .audit import emit as emit_audit
    from .models import PermissionAuditEvent

    if action == "delete":
        kind = PermissionAuditEvent.KIND_RELATIONSHIP_REVOKE
    else:
        kind = PermissionAuditEvent.KIND_RELATIONSHIP_GRANT
    emit_audit(
        kind,
        actor=actor,
        origin=actor,
        target_repr=f"{resource}#{action}",
        reason=f"denied: {actor} cannot {action} {resource}",
        defer_to_commit=False,
    )


@receiver(pre_save)
def _rebac_pre_save(
    sender: type,
    instance: Any,
    raw: bool = False,
    using: Any = None,
    update_fields: Iterable[str] | None = None,
    **_: Any,
) -> None:
    if raw:
        return
    if not isinstance(instance, RebacMixin):
        return
    rebac_type = getattr(sender._meta, "rebac_resource_type", None)
    if not rebac_type:
        return
    # Per-instance sudo (set by `instance.sudo(reason=...)`) bypasses the
    # check just like the ambient ContextVar. Per CLAUDE.md § 5a, the flag
    # is non-transitive — it lives on this instance only and does not
    # propagate to FK / M2M accessors.
    if _is_sudo() or getattr(instance, "_rebac_sudo_reason", None) is not None:
        return

    # Resolve actor: per-instance (set by from_db / queryset / .with_actor) → ambient.
    actor = getattr(instance, "_rebac_actor", None) or _current_actor()
    if actor is None:
        if app_settings.REBAC_STRICT_MODE:
            raise MissingActorError(
                f"{sender.__name__}.save() called with no actor. "
                f"Use a queryset scoped via .with_actor()/.as_user()/.as_agent(), "
                f"or wrap in `with sudo(reason='...'):`."
            )
        return

    is_create = instance._state.adding
    action = "create" if is_create else "write"

    from .backends import backend

    # Empty resource_id on create — even when the configured attr is
    # something like ``sqid`` (a virtual field computed from PK), the
    # value isn't computable until after the insert. Same sentinel as
    # the pk-default path.
    if is_create:
        resource_id = ""
    else:
        resource_id = str(getattr(instance, resource_id_attr(sender)))
    resource = ObjectRef(rebac_type, resource_id)
    result = backend().check_access(subject=actor, action=action, resource=resource)
    if not result.allowed:
        _maybe_audit_denial(actor=actor, action=action, resource=resource)
        raise PermissionDenied(f"Denied: {actor} cannot {action} {resource}")

    # Per-field ``write__<f>`` enforcement — only on UPDATE. On INSERT the
    # row didn't exist, so "loaded values" is empty and every field is
    # trivially "dirty"; gating create on per-field permissions makes no
    # sense (use ``permission create = ...`` for that).
    if not is_create:
        _enforce_per_field_writes(
            sender=sender,
            instance=instance,
            actor=actor,
            resource=resource,
            update_fields=update_fields,
        )


@receiver(pre_delete)
def _rebac_pre_delete(sender: type, instance: Any, using: Any = None, **_: Any) -> None:
    if not isinstance(instance, RebacMixin):
        return
    rebac_type = getattr(sender._meta, "rebac_resource_type", None)
    if not rebac_type:
        return
    if _is_sudo() or getattr(instance, "_rebac_sudo_reason", None) is not None:
        return

    actor = getattr(instance, "_rebac_actor", None) or _current_actor()
    if actor is None:
        if app_settings.REBAC_STRICT_MODE:
            raise MissingActorError(f"{sender.__name__}.delete() called with no actor.")
        return

    from .backends import backend

    resource_id = str(getattr(instance, resource_id_attr(sender)))
    resource = ObjectRef(rebac_type, resource_id)
    result = backend().check_access(subject=actor, action="delete", resource=resource)
    if not result.allowed:
        _maybe_audit_denial(actor=actor, action="delete", resource=resource)
        raise PermissionDenied(f"Denied: {actor} cannot delete {resource}")


# ---------- Per-field write helpers ----------


def _enforce_per_field_writes(
    *,
    sender: type,
    instance: Any,
    actor: SubjectRef,
    resource: ObjectRef,
    update_fields: Iterable[str] | None,
) -> None:
    """Re-run ``check_access`` for any dirty field that has a ``write__<f>``
    permission declared on its resource type.

    Called after the resource-level ``write`` check has already passed.
    Honours ``save(update_fields=...)`` when supplied (the caller knows
    what's actually dirty); otherwise falls back to comparing current
    values against the snapshot ``from_db`` stashed on the instance. If
    no snapshot is present (instance hand-built and re-saved as an
    UPDATE — unusual), every non-pk concrete field is treated as dirty
    (conservative; fail-closed).

    Schema lookup goes via ``backend().schema()`` when the backend
    exposes one (LocalBackend always does; SpiceDBBackend will route
    through its own server-side schema once 0.5 lands). Backends without
    an in-process schema accessor skip per-field enforcement — the
    resource-level ``write`` check already gated the operation.

    Pure in-memory comparison; never queries the DB to refresh state.
    """
    schema = _backend_schema()
    if schema is None:
        return
    definition = schema.get_definition(resource.resource_type)
    if definition is None:
        return
    declared = {p.name for p in definition.permissions if p.name.startswith("write__")}
    if not declared:
        return  # No per-field gates declared — common case, cheap exit.

    dirty = _dirty_field_names(sender=sender, instance=instance, update_fields=update_fields)
    if not dirty:
        return

    from .backends import backend

    for field_name in dirty:
        action = f"write__{field_name}"
        if action not in declared:
            continue  # Field inherits the resource-level write (already passed).
        result = backend().check_access(subject=actor, action=action, resource=resource)
        if not result.allowed:
            _maybe_audit_denial(actor=actor, action=action, resource=resource)
            raise PermissionDenied(
                f"Denied: {actor} cannot {action} {resource} "
                f"(field {field_name!r} requires {action})"
            )


def _backend_schema() -> Schema | None:
    """Best-effort: return the backend's in-memory ``Schema`` if it has one.

    LocalBackend exposes ``.schema()``; SpiceDBBackend (post-0.5) won't
    use an in-process tree the same way, so callers must handle ``None``.
    """
    from .backends import backend

    accessor = getattr(backend(), "schema", None)
    if not callable(accessor):
        return None
    try:
        result = accessor()
    except Exception:
        return None
    return result if isinstance(result, Schema) else None


def _dirty_field_names(
    *,
    sender: type,
    instance: Any,
    update_fields: Iterable[str] | None,
) -> list[str]:
    """Return the list of (field.name) values that have changed.

    Trust order:

    1. If the caller passed ``save(update_fields=[...])``, trust it
       (Django itself only writes those columns). Normalise tokens to
       ``field.name`` so that both ``"folder"`` and ``"folder_id"`` look
       up ``write__folder`` correctly.
    2. Otherwise compare ``_rebac_loaded_values`` (snapshotted in
       ``RebacMixin.from_db``) against the current attribute values for
       every non-pk concrete field. Fields that were deferred at load
       time (absent from the snapshot) are treated as dirty.
    3. If no snapshot exists at all (e.g. hand-built instance being
       re-saved as an UPDATE — rare), conservatively treat every non-pk
       concrete field as dirty.
    """
    meta = sender._meta
    if update_fields is not None:
        names: list[str] = []
        for tok in update_fields:
            try:
                field = meta.get_field(tok)
            except Exception:
                # Unknown field tag — leave it; Django will reject the save.
                names.append(tok)
                continue
            names.append(field.name)
        return names

    concrete = [f for f in meta.concrete_fields if not f.primary_key]
    loaded: dict[str, Any] | None = getattr(instance, "_rebac_loaded_values", None)
    if loaded is None:
        return [f.name for f in concrete]

    dirty: list[str] = []
    for field in concrete:
        attname = field.attname
        current = getattr(instance, attname, None)
        if attname not in loaded:
            # Was deferred at load time — can't compare cheaply.
            dirty.append(field.name)
        elif loaded[attname] != current:
            dirty.append(field.name)
    return dirty


# ---------------------------------------------------------------------------
# Schema cache invalidation + SchemaOverride audit
# ---------------------------------------------------------------------------
#
# Schema* CRUD must invalidate DB-loaded LocalBackend schemas so the next
# permission check rebuilds the in-memory schema without paying schema-table
# fingerprint queries on the hot path. Tier-2 override CRUD also resets the
# cached global backend and emits a PermissionAuditEvent via the single
# audit-emission helper. Single-process only — multi-process LISTEN/NOTIFY is
# a v1.x roadmap item.


def _mark_schema_caches_stale() -> None:
    from .backends.local import mark_db_loaded_schemas_stale

    mark_db_loaded_schemas_stale()


@receiver(post_save, sender="rebac.SchemaDefinition")
@receiver(post_delete, sender="rebac.SchemaDefinition")
@receiver(post_save, sender="rebac.SchemaRelation")
@receiver(post_delete, sender="rebac.SchemaRelation")
@receiver(post_save, sender="rebac.SchemaPermission")
@receiver(post_delete, sender="rebac.SchemaPermission")
@receiver(post_save, sender="rebac.SchemaCaveat")
@receiver(post_delete, sender="rebac.SchemaCaveat")
def _rebac_schema_rows_changed(sender: type, raw: bool = False, **_: Any) -> None:
    if raw:
        return
    _mark_schema_caches_stale()


def _override_target_repr(instance: Any) -> str:
    """Best-effort string repr of the override target for audit rows."""
    from django.contrib.contenttypes.models import ContentType

    try:
        ct = instance.target_ct
        return f"{instance.kind}:{ct.app_label}.{ct.model}/{instance.target_pk}"
    except ContentType.DoesNotExist:
        return f"{getattr(instance, 'kind', '?')}:?/{getattr(instance, 'target_pk', '?')}"


def _override_payload(instance: Any) -> dict[str, Any]:
    return {
        "kind": getattr(instance, "kind", ""),
        "expression": getattr(instance, "expression", ""),
        "reason": getattr(instance, "reason", ""),
    }


def _emit_override_audit(
    *, kind: str, instance: Any, before: dict[str, Any] | None, after: dict[str, Any] | None
) -> None:
    """Emit an override.* PermissionAuditEvent via the single emission point."""
    from .audit import emit as emit_audit

    actor = _current_actor()
    emit_audit(
        kind,
        actor=actor,
        origin=actor,
        target_repr=_override_target_repr(instance),
        before=before,
        after=after,
        reason=getattr(instance, "reason", "") or "",
        defer_to_commit=True,
    )


@receiver(post_save, sender="rebac.SchemaOverride")
def _rebac_override_post_save(
    sender: type, instance: Any, created: bool, raw: bool = False, **_: Any
) -> None:
    if raw:
        return
    from .backends import reset_backend
    from .models import PermissionAuditEvent

    _mark_schema_caches_stale()
    reset_backend()
    if created:
        _emit_override_audit(
            kind=PermissionAuditEvent.KIND_OVERRIDE_CREATE,
            instance=instance,
            before=None,
            after=_override_payload(instance),
        )
    # Updates to existing overrides aren't separately audited in v1; treat
    # them as configuration drift and rely on the underlying admin log.


@receiver(post_delete, sender="rebac.SchemaOverride")
def _rebac_override_post_delete(sender: type, instance: Any, **_: Any) -> None:
    from .backends import reset_backend
    from .models import PermissionAuditEvent

    _mark_schema_caches_stale()
    reset_backend()
    _emit_override_audit(
        kind=PermissionAuditEvent.KIND_OVERRIDE_DELETE,
        instance=instance,
        before=_override_payload(instance),
        after=None,
    )


# ---------- Proposal 0001: RebacResource cascade ----------
#
# When a Django row backed by ``RebacMixin`` is deleted in registry mode,
# its corresponding ``RebacResource`` row must die with it so the
# ``RelationshipRegistry.resource_fk`` / ``subject_fk`` CASCADE constraint
# can sweep every tuple it appeared in. Without this handler the registry
# row would be orphaned and tuples would persist past the underlying
# resource's lifetime — exactly the leak the registry shape was meant to
# fix.
#
# In denormalized mode the handler is a no-op (there are no FKs to
# cascade through); callers do their own ``Relationship.objects.filter(
# resource_type=..., resource_id=...).delete()`` post_delete sweep when
# they need it.


@receiver(post_delete)
def _rebac_cascade_resource(sender: type, instance: Any, **_: Any) -> None:
    """Drop the ``RebacResource`` registry row for a deleted ``RebacMixin`` row.

    Listens on every model's ``post_delete``; short-circuits in O(1) when
    the sender is not REBAC-bound (the ``getattr(meta,
    rebac_resource_type, None)`` lookup is the only work done in the
    common-case false branch).
    """
    if app_settings.REBAC_LOCAL_BACKEND_STORAGE != "registry":
        return
    if not isinstance(instance, RebacMixin):
        return
    rebac_type = getattr(sender._meta, "rebac_resource_type", None)
    if not rebac_type:
        return
    # Lazy import — ``RebacResource`` lives in ``rebac.models`` which is
    # available by signal-fire time but importing eagerly would be a
    # heavy top-level dependency for a no-op in denormalized mode.
    from .models import RebacResource

    resource_id = str(getattr(instance, resource_id_attr(sender)))
    RebacResource.objects.filter(
        resource_type=rebac_type,
        resource_id=resource_id,
    ).delete()
