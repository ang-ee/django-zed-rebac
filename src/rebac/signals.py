"""Pre-save / pre-delete signal handlers gating writes through REBAC."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db.models import Model, Q
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from ._id import resource_id_attr
from .actors import current_actor as _current_actor
from .actors import model_can_resolve_subject, to_subject_ref
from .conf import app_settings
from .errors import NoActorResolvedError, PermissionDenied
from .field_visibility import backend_schema
from .mixins import RebacMixin
from .preflight import _check_new_model
from .resources import model_resource_type, to_object_ref
from .schema.walker import field_gated_actions
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
    sender: type[Model],
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
    rebac_type = model_resource_type(sender)
    if not rebac_type:
        return
    # Share the observer/check API's precedence: a pinned actor outranks
    # ambient sudo, while an explicit instance bypass still wins locally.
    actor, unscoped = instance.effective_actor(strict=True)
    if unscoped:
        return
    assert actor is not None

    is_create = instance._state.adding
    action = "create" if is_create else "write"

    from .backends import backend

    if is_create:
        active_backend = backend()
        result = _check_new_model(
            instance,
            subject=actor,
            using=using,
            backend=active_backend,
        )
        resource = ObjectRef(rebac_type, "")
    else:
        resource_id = _resource_id_for_existing_instance(sender=sender, instance=instance)
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
        _enforce_redacted_field_writes(
            sender=sender,
            instance=instance,
            resource=resource,
            update_fields=update_fields,
        )
        _enforce_per_field_writes(
            sender=sender,
            instance=instance,
            actor=actor,
            resource=resource,
            update_fields=update_fields,
        )


@receiver(pre_delete)
def _rebac_pre_delete(sender: type[Model], instance: Any, using: Any = None, **_: Any) -> None:
    if not isinstance(instance, RebacMixin):
        return
    rebac_type = model_resource_type(sender)
    if not rebac_type:
        return
    actor, unscoped = instance.effective_actor(strict=True)
    if unscoped:
        return
    assert actor is not None

    from .backends import backend

    resource_id = str(getattr(instance, resource_id_attr(sender)))
    resource = ObjectRef(rebac_type, resource_id)
    result = backend().check_access(subject=actor, action="delete", resource=resource)
    if not result.allowed:
        _maybe_audit_denial(actor=actor, action="delete", resource=resource)
        raise PermissionDenied(f"Denied: {actor} cannot delete {resource}")


# ---------- Per-field write helpers ----------


def _enforce_redacted_field_writes(
    *,
    sender: type[Model],
    instance: Any,
    resource: ObjectRef,
    update_fields: Iterable[str] | None,
) -> None:
    redacted = frozenset(getattr(instance, "_rebac_redacted_fields", frozenset()) or frozenset())
    if not redacted or update_fields is None:
        return
    requested = set(_normalise_update_field_names(sender=sender, update_fields=update_fields))
    bad = redacted & requested
    if bad:
        names = ", ".join(sorted(bad))
        raise PermissionDenied(
            f"Cannot write redacted field(s) {names} on {resource}: "
            "read__<field> denied on the loaded instance."
        )


def _resource_id_for_existing_instance(*, sender: type[Model], instance: Any) -> str:
    attr = resource_id_attr(sender)
    redacted = frozenset(getattr(instance, "_rebac_redacted_fields", frozenset()) or frozenset())
    field_names = {attr}
    try:
        field = sender._meta.get_field(attr)
    except Exception:
        field = None
    if field is not None:
        field_names.add(field.name)
        field_names.add(getattr(field, "attname", field.name))
    if redacted & field_names:
        stored = getattr(instance, "_rebac_resource_id", None)
        if stored is not None:
            return str(stored)
    return str(getattr(instance, attr))


def _enforce_per_field_writes(
    *,
    sender: type[Model],
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
    schema = backend_schema()
    if schema is None:
        return
    definition = schema.get_definition(resource.resource_type)
    if definition is None:
        return
    declared = field_gated_actions(definition, "write")
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


def _dirty_field_names(
    *,
    sender: type[Model],
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
        return _normalise_update_field_names(sender=sender, update_fields=update_fields)

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


def _normalise_update_field_names(
    *,
    sender: type[Model],
    update_fields: Iterable[str],
) -> list[str]:
    meta = sender._meta
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


# ---------------------------------------------------------------------------
# Schema cache invalidation + SchemaOverride audit
# ---------------------------------------------------------------------------
#
# Schema* CRUD invalidates DB-loaded LocalBackend schemas within the current
# process and scope. New request/backend-operation scopes also reload schema
# rows, so another worker's commits do not require a process-local signal.
# Tier-2 override CRUD also resets the cached global backend and emits a
# PermissionAuditEvent via the single audit-emission helper.


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
def _rebac_schema_rows_changed(sender: type[Model], raw: bool = False, **_: Any) -> None:
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
    sender: type[Model], instance: Any, created: bool, raw: bool = False, **_: Any
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
def _rebac_override_post_delete(sender: type[Model], instance: Any, **_: Any) -> None:
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


# ---------- RebacResource cascade ----------
#
# When a Django row with a resource or subject identity is deleted, every
# relationship occurrence of that identity must disappear with it. Registry
# storage does so by deleting its ``RebacResource`` row and relying on both FK
# cascades.
#
# Denormalized storage has no foreign keys, so this handler deletes both
# resource-side and subject-side occurrences explicitly. Both paths run on the
# deleting instance's database alias inside Django's own delete transaction.


@receiver(post_delete)
def _rebac_cascade_resource(
    sender: type[Model], instance: Any, using: str = "default", **_: Any
) -> None:
    """Remove every relationship occurrence of a deleted model identity.

    Listens on every model's ``post_delete`` and uses class metadata to skip
    rows for which neither the resource nor actor resolver owns an identity.
    This includes configured Django User/Group subjects as well as RebacMixin
    resources and model-owned or explicitly registered subjects.
    """
    identities: set[ObjectRef] = set()
    if isinstance(instance, RebacMixin) and model_resource_type(sender):
        identities.add(to_object_ref(instance))
    if model_can_resolve_subject(sender):
        try:
            identities.add(to_subject_ref(instance).object)
        except NoActorResolvedError:
            pass
    if not identities:
        return
    from .backends.local import mark_relationships_changed

    # ``post_delete`` fires inside the deleting Collector's atomic block on
    # ``using``; the queryset deletes below join that transaction.
    if app_settings.REBAC_LOCAL_BACKEND_STORAGE == "registry":
        from .models import RebacResource

        identity_filter = Q()
        for identity in sorted(identities, key=lambda ref: (ref.resource_type, ref.resource_id)):
            identity_filter |= Q(
                resource_type=identity.resource_type,
                resource_id=identity.resource_id,
            )
        RebacResource.objects.using(using).filter(identity_filter).delete()
    else:
        from .models import active_relationship_model

        relationship_filter = Q()
        for identity in sorted(identities, key=lambda ref: (ref.resource_type, ref.resource_id)):
            relationship_filter |= Q(
                resource_type=identity.resource_type,
                resource_id=identity.resource_id,
            ) | Q(
                subject_type=identity.resource_type,
                subject_id=identity.resource_id,
            )
        active_relationship_model().objects.using(using).filter(relationship_filter).delete()
    # The rows changed outside the backend's own write path; drop decisions.
    mark_relationships_changed()
