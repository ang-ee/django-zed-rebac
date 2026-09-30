"""Explicit-sender lifecycle hooks for writes that bypass model/queryset owners."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import batched
from typing import Any, cast
from weakref import WeakSet

from django.apps import apps
from django.db import models, router
from django.db.models import Model, Q
from django.db.models.signals import (
    class_prepared,
    m2m_changed,
    post_delete,
    post_save,
    pre_delete,
    pre_save,
)
from django.dispatch import receiver

from .actors import model_can_resolve_subject, to_subject_ref
from .conf import app_settings
from .errors import NoActorResolvedError
from .resources import model_resource_type, to_object_ref
from .types import ObjectRef, SubjectRef

# Weak sets retain no isolated app registry after its classes disappear.
_owned: WeakSet[type[Model]] = WeakSet()
_subjects: WeakSet[type[Model]] = WeakSet()
_tracked: WeakSet[type[Model]] = WeakSet()
_throughs: WeakSet[type[Model]] = WeakSet()
_through_candidates: WeakSet[type[Model]] = WeakSet()


def tracked_model(model: type[Model]) -> bool:
    """Static tracking eligibility; no schema/backend/database access."""
    from django.conf import settings

    from .mixins import RebacTrackedMixin

    if issubclass(model, RebacTrackedMixin):
        return True
    labels = {settings.AUTH_USER_MODEL.lower(), "auth.group"}
    configured = app_settings.REBAC_TRACKED_MODELS
    if isinstance(configured, (list, tuple)):
        labels.update(label.lower() for label in configured if isinstance(label, str))
    return any(
        hasattr(base, "_meta") and base._meta.label_lower in labels for base in model.__mro__
    )


def tracked_through(model: type[Model]) -> bool:
    return bool(model._meta.auto_created) and any(
        isinstance(field.remote_field.model, type) and tracked_model(field.remote_field.model)
        for field in model._meta.fields
        if isinstance(field, models.ForeignKey)
    )


def _connect(signal: Any, handler: Any, model: type[Model]) -> None:
    # Only callers that just changed membership connect. Repeated connect()
    # would clear Django's project-wide per-sender cache even with a stable UID.
    signal.connect(handler, sender=model, weak=False, dispatch_uid=handler.__name__)


def connect_owned_model(model: type[Model]) -> None:
    if model._meta.abstract or model in _owned:
        return
    _owned.add(model)
    _connect(pre_delete, _rebac_pre_delete, model)
    connect_subject_model(model)


def connect_subject_model(model: type[Model]) -> None:
    if model._meta.abstract or model in _subjects:
        return
    _subjects.add(model)
    _connect(post_delete, _rebac_cascade_resource, model)
    # Decorators run after class_prepared. Cover any subclasses already present.
    for child in model.__subclasses__():
        if hasattr(child, "_meta"):
            connect_subject_model(child)


def _register_model(model: type[Model]) -> None:
    from .actors import _subject_registry
    from .mixins import RebacTrackedMixin

    if model._meta.abstract:
        return
    # Owned classes connect in RebacModelBase after metadata is installed.
    if not issubclass(model, RebacTrackedMixin) and tracked_model(model):
        if model not in _tracked:
            _tracked.add(model)
            _connect(pre_save, _index_pre_save, model)
            _connect(post_save, _index_post_save, model)
            _connect(pre_delete, _rebac_pre_delete, model)
            connect_subject_model(model)  # also finishes non-identity tracked deletes
    # Avoid get_user_model() during class preparation; labels/lineage suffice.
    if any(base in _subject_registry for base in model.__mro__):
        connect_subject_model(model)
    if model._meta.auto_created:
        _through_candidates.add(model)
    for through in tuple(_through_candidates):
        eligible = tracked_through(through)
        if not eligible and through._meta.apps is model._meta.apps and tracked_model(model):
            # class_prepared runs just before registry insertion and lazy FK
            # resolution. Match a pending string endpoint to this known class.
            eligible = any(
                isinstance(field.remote_field.model, str)
                and (
                    field.remote_field.model
                    if "." in field.remote_field.model
                    else f"{through._meta.app_label}.{field.remote_field.model}"
                ).lower()
                == model._meta.label_lower
                for field in through._meta.fields
                if isinstance(field, models.ForeignKey)
            )
        if eligible and through not in _throughs:
            _throughs.add(through)
            _connect(m2m_changed, _index_m2m, through)


def connect_tracked_signals() -> None:
    """Install the static sender superset from the registry only."""
    # Setting overrides may remove tracked classes; update only changed senders.
    for model in tuple(_tracked):
        if not tracked_model(model):
            for signal, handler in (
                (pre_save, _index_pre_save),
                (post_save, _index_post_save),
                (pre_delete, _rebac_pre_delete),
            ):
                signal.disconnect(sender=model, dispatch_uid=handler.__name__)
            _tracked.discard(model)
            if not model_can_resolve_subject(model):
                post_delete.disconnect(sender=model, dispatch_uid="_rebac_cascade_resource")
                _subjects.discard(model)
    for model in tuple(_throughs):
        if not tracked_through(model):
            m2m_changed.disconnect(sender=model, dispatch_uid="_index_m2m")
            _throughs.discard(model)
    for model in apps.get_models(include_auto_created=True):
        _register_model(model)


@receiver(class_prepared, dispatch_uid="rebac.model_prepared")
def _model_prepared(sender: type[Model], **kwargs: Any) -> None:
    _register_model(sender)
    # A through model can be prepared before its endpoint finishes resolving.
    # Revisit local M2M fields once the endpoint is prepared (no registry query).
    for field in sender._meta.local_many_to_many:
        through = field.remote_field.through
        if isinstance(through, type):
            _register_model(through)


def _current_watch(
    sender: type[Model],
    using: str,
    names: Iterable[str] | None = None,
) -> bool:
    from .backends import backend
    from .backends.local import LocalBackend
    from .index.maintain import current_pass, get_program, installed, model_is_watched
    from .models import active_relationship_model

    if not router.allow_migrate_model(using, active_relationship_model()):
        return False
    active = backend()
    if not isinstance(active, LocalBackend):
        return False
    outer = current_pass(using)
    if outer is not None:
        return outer.watches(sender, names)
    if not installed(using, active):
        return False
    return model_is_watched(get_program(using, active).watched, sender, names)


def _identities(sender: type[Model], instance: Any) -> set[ObjectRef]:
    identities: set[ObjectRef] = set()
    if model_resource_type(sender):
        identities.add(to_object_ref(instance))
    if model_can_resolve_subject(sender):
        try:
            identities.add(to_subject_ref(instance).object)
        except NoActorResolvedError:
            pass
    return identities


def cleanup_identities(identities: Iterable[ObjectRef], *, using: str) -> None:
    """Batch both sides of every identity inside the deletion owner's pass."""
    from .backends.local import mark_relationships_changed
    from .index.maintain import tuple_owner
    from .models import RebacResource, active_relationship_model
    from .models.relationship import engine_tuple_write, projected_tuples

    identities = sorted(set(identities), key=str)
    if not identities or not router.allow_migrate_model(using, active_relationship_model()):
        return
    with tuple_owner(using) as maintenance:
        for batch in batched(identities, 200, strict=False):
            references = Q()
            registry = Q()
            for identity in batch:
                pair = Q(resource_type=identity.resource_type, resource_id=identity.resource_id)
                registry |= pair
                references |= pair | Q(
                    subject_type=identity.resource_type, subject_id=identity.resource_id
                )
            rows = active_relationship_model().objects.using(using).filter(references)
            if maintenance is not None:
                maintenance.capture_old(tuples=projected_tuples(cast(Any, rows).index_projection()))
            with engine_tuple_write():
                if app_settings.REBAC_LOCAL_BACKEND_STORAGE == "registry":
                    RebacResource.objects.using(using).filter(registry).delete()
                else:
                    rows.delete()
        mark_relationships_changed()


def _rebac_pre_delete(
    sender: type[Model],
    instance: Any,
    using: str,
    origin: Any = None,
    **kwargs: Any,
) -> None:
    from .managers import RebacQuerySet, TrackedQuerySet
    from .mixins import RebacMixin, _gate_delete, delete_scope

    scope = delete_scope(origin, using)
    # Scoped roots were gated by their owner; base-manager roots are explicitly
    # unscoped. Only rows actually covered by that root operation can skip.
    covered = scope is not None and (
        origin is instance
        or (
            isinstance(origin, (RebacQuerySet, TrackedQuerySet))
            and origin.model is sender
            and instance.pk in scope.root_pks
        )
    )
    if isinstance(instance, RebacMixin) and not covered:
        _gate_delete(
            sender,
            instance,
            scope=(scope.actor, scope.unscoped) if scope is not None else None,
        )
    if not covered:
        _capture_signal_old(sender, instance, using)


def _rebac_cascade_resource(
    sender: type[Model],
    instance: Any,
    using: str = "default",
    origin: Any = None,
    **kwargs: Any,
) -> None:
    from .mixins import delete_scope

    scope = delete_scope(origin, using)
    identities = _identities(sender, instance)
    if scope is not None:
        scope.identities.update(identities)
        return  # the owner holds the old captures and finishes once for the collection
    else:
        cleanup_identities(identities, using=using)
    if sender in _owned or sender in _tracked:
        _finish_signal(sender, instance, using)


@receiver(post_delete, sender="rebac.SchemaOverride")
def _rebac_schema_cascade_revision(
    sender: type[Model], *, instance: Any, origin: Any = None, using: str, **_: Any
) -> None:
    """Cover collector cascades whose origin does not own schema publication."""
    from .models.generation import SchemaGeneration
    from .models.schema_write import SchemaQuerySet, SchemaRow, _publish, schema_index_write

    if not isinstance(origin, (SchemaRow, SchemaQuerySet)):
        from .index.maintain import forget_signal_pass
        from .models.index import IndexWork

        with schema_index_write(using) as maintenance:
            old_pass = instance.__dict__.pop("_rebac_index_schema_pass", None)
            if old_pass is not None:
                IndexWork.objects.using(using).filter(pass_id=old_pass).delete()
                forget_signal_pass(using, old_pass)
            SchemaGeneration.objects.advance(using=using)
            _publish(maintenance, None)
            instance._audit_change(created=False)


@receiver(pre_delete, sender="rebac.SchemaOverride")
def _rebac_schema_cascade_begin(
    sender: type[Model], *, instance: Any, using: str, origin: Any = None, **_: Any
) -> None:
    from django.db import connections

    from .index.maintain import IndexMaintenance, current_pass, defer_signal_pass
    from .models.schema_write import SchemaQuerySet, SchemaRow, _old_program

    if isinstance(origin, (SchemaRow, SchemaQuerySet)) or current_pass(using) is not None:
        return
    instance._audit_target = instance._audit_target_repr()
    caller_atomic = (
        connections[using].atomic_blocks[-1] if connections[using].in_atomic_block else None
    )
    with IndexMaintenance(using=using) as maintenance:
        _old_program(maintenance)
        maintenance.deferred = True
        instance.__dict__["_rebac_index_schema_pass"] = maintenance.pass_id
        defer_signal_pass(maintenance, caller_atomic)


def _plain_write_warning(sender: type[Model], using: str, *, in_atomic: bool) -> None:
    import warnings

    from .index.maintain import logger

    if not in_atomic:
        message = f"{sender._meta.label} changes permission backing fields outside atomic(using={using!r}); source and index cannot roll back together (D2)."
        logger.error(message)
        warnings.warn(message, RuntimeWarning, stacklevel=4)


def _capture_signal_old(
    sender: type[Model],
    instance: Any,
    using: str,
    *,
    names: Iterable[str] | None = None,
    warn: bool = False,
) -> None:
    from django.db import connections

    from .index.maintain import IndexMaintenance, current_pass, defer_signal_pass

    if not _current_watch(sender, using, names):
        return
    was_atomic = connections[using].in_atomic_block
    caller_atomic = connections[using].atomic_blocks[-1] if was_atomic else None
    outer = current_pass(using)
    with IndexMaintenance(using=using) as maintenance:
        if not maintenance.watches(sender, names):
            return
        if warn:
            instance.__dict__["_rebac_index_plain_atomic"] = was_atomic
        maintenance.capture_old(model=sender, pks=(instance.pk,))
        if outer is None:
            # Do not hold an unmanaged context manager across Django's signal
            # dispatch: a failed save never sends post_save. The enclosing
            # caller transaction retains the row lock and rolls this work back.
            maintenance.deferred = True
            instance.__dict__["_rebac_index_old_pass"] = maintenance.pass_id
            defer_signal_pass(maintenance, caller_atomic)


def _finish_signal(
    sender: type[Model], instance: Any, using: str, *, names: Iterable[str] | None = None
) -> None:
    from .index.maintain import IndexMaintenance, forget_signal_pass
    from .models.index import IndexWork

    old_pass = instance.__dict__.pop("_rebac_index_old_pass", None)
    if old_pass is None and not _current_watch(sender, using, names):
        return
    with IndexMaintenance(using=using, resume_pass=old_pass) as maintenance:
        if old_pass is not None:
            IndexWork.objects.using(using).filter(pass_id=old_pass).exclude(kind="pass").update(
                pass_id=maintenance.pass_id
            )
            IndexWork.objects.using(using).filter(pass_id=old_pass).delete()
            forget_signal_pass(using, old_pass)
        if maintenance.watches(sender, names):
            maintenance.changed(model=sender, pks=(instance.pk,))


def _index_pre_save(
    sender: type[Model],
    instance: Any,
    using: str,
    raw: bool = False,
    update_fields: Iterable[str] | None = None,
    **kwargs: Any,
) -> None:
    if not raw:
        _gate_backed_field_change(sender, instance, using, update_fields)
        _capture_signal_old(sender, instance, using, names=update_fields, warn=True)


def _index_post_save(
    sender: type[Model],
    instance: Any,
    using: str,
    raw: bool = False,
    update_fields: Iterable[str] | None = None,
    **kwargs: Any,
) -> None:
    if not raw:
        _finish_signal(sender, instance, using, names=update_fields)
        was_atomic = instance.__dict__.pop("_rebac_index_plain_atomic", True)
        _plain_write_warning(sender, using, in_atomic=was_atomic)


def _index_m2m(
    sender: type[Model],
    instance: Any,
    action: str,
    reverse: bool,
    model: type[Model],
    pk_set: set[Any] | None,
    using: str,
    **kwargs: Any,
) -> None:
    if sender not in _throughs:
        return
    if action in {"pre_add", "pre_remove", "pre_clear"}:
        _gate_m2m(sender, instance, reverse, model, pk_set, using)
    if not _current_watch(sender, using):
        return
    if action in {"pre_add", "pre_remove", "pre_clear"}:
        _capture_signal_old(type(instance), instance, using)
    elif action in {"post_add", "post_remove", "post_clear"}:
        _finish_signal(type(instance), instance, using)


def _gate_m2m(
    sender: type[Model],
    instance: Any,
    reverse: bool,
    model: type[Model],
    pk_set: set[Any] | None,
    using: str,
) -> None:
    """Gate each resource endpoint owning an affected M2M-backed edge."""
    from .index.maintain import current_pass, get_program
    from .resources import model_for_resource_type

    del reverse
    owner_model = sender._meta.auto_created
    if not isinstance(owner_model, type):
        return
    owner_type = model_resource_type(owner_model)
    outer = current_pass(using)
    program = outer.load_program() if outer is not None else get_program(using)
    watched = (
        program.watched.get(sender._meta.label_lower) if _current_watch(sender, using) else None
    )
    types = set(watched.resource_types if watched is not None else ())
    if owner_type is not None:
        types.add(owner_type)
    if not types or pk_set == set():
        return
    actor, bypass = _edge_actor(instance)
    if bypass:
        return
    fields = [field for field in sender._meta.fields if isinstance(field, models.ForeignKey)]
    instance_model = instance._meta.concrete_model
    instance_field = next(
        field for field in fields if field.remote_field.model._meta.concrete_model is instance_model
    )
    other_field = next(field for field in fields if field is not instance_field)
    other_ids = (
        pk_set
        if pk_set is not None
        else set(
            sender._base_manager.using(using)
            .filter(**{instance_field.attname: instance.pk})
            .values_list(other_field.attname, flat=True)
        )
    )
    owner_pks: dict[type[Model], set[Any]] = {instance_model: {instance.pk}}
    other_model = model._meta.concrete_model
    assert other_model is not None
    owner_pks.setdefault(other_model, set()).update(other_ids)
    backing_ids = _affected_backing_ids(
        program, using=using, owner_pks=owner_pks, through=sender, resource_types=types
    )
    for resource_type in sorted(types):
        resource_model = model_for_resource_type(resource_type)
        ids: set[str] = backing_ids.get(resource_type, set())
        if resource_model is not None and resource_model._meta.concrete_model is instance_model:
            ids.add(to_object_ref(instance).resource_id)
        if (
            resource_model is not None
            and resource_model._meta.concrete_model is model._meta.concrete_model
        ):
            for row in resource_model._base_manager.using(using).filter(pk__in=other_ids):
                ids.add(to_object_ref(row).resource_id)
        if ids:
            _check_edge_writes(actor, resource_type, ids)


def _affected_backing_ids(
    program: Any,
    *,
    using: str,
    owner_pks: dict[type[Model], set[Any]],
    resource_types: set[str],
    through: type[Model] | None = None,
    changed_field: models.Field[Any, Any] | None = None,
    proposed: Model | None = None,
) -> dict[str, set[str]]:
    """Find source rows reached through a changed hop, including nested paths."""
    from .field_backing import (
        resolve_attribute_backing,
        resolve_const_backing,
        resolve_field_backing,
    )

    affected: dict[str, set[str]] = {}
    for definition in program.baseline.definitions:
        type_ = definition.resource_type
        if type_ not in resource_types:
            continue
        for relation in definition.relations:
            backing = resolve_field_backing(definition, relation)
            attribute = resolve_attribute_backing(definition, relation)
            const = resolve_const_backing(definition, relation)
            if backing is not None:
                source = backing.source_model
                paths = (backing.path, *backing.filters)
            elif attribute is not None:
                source = attribute.target_model
                paths = (attribute.field.name, *attribute.filters)
            elif const is not None:
                source = const.source_model
                paths = tuple(const.filters)
            else:
                continue
            for prefix in sorted(
                _changed_hop_prefixes(
                    source,
                    paths,
                    owner_pks,
                    through,
                    changed_field,
                )
            ):
                owner = source
                for part in prefix.split("__") if prefix else ():
                    related = owner._meta.get_field(part).related_model
                    assert related is not None
                    owner = related
                concrete = owner._meta.concrete_model
                assert concrete is not None
                pks = owner_pks[concrete]
                lookup = f"{prefix}__pk__in" if prefix else "pk__in"
                for row in source._base_manager.using(using).filter(**{lookup: pks}):
                    resource_id: str | None
                    if backing is not None or const is not None:
                        resource_id = to_object_ref(row).resource_id
                    else:
                        assert attribute is not None
                        value = getattr(row, attribute.field.attname)
                        if attribute.resource is None:
                            resource_id = attribute.container_id_of(value)
                        else:
                            resource_id = attribute.resource if value == attribute.value else None
                    if resource_id is not None:
                        affected.setdefault(type_, set()).add(resource_id)
            if (
                attribute is not None
                and proposed is not None
                and source._meta.concrete_model is proposed._meta.concrete_model
                and changed_field is attribute.field
            ):
                value = getattr(proposed, attribute.field.attname)
                if attribute.resource is None:
                    new_id = attribute.container_id_of(value)
                else:
                    new_id = attribute.resource if value == attribute.value else None
                if new_id is not None:
                    affected.setdefault(type_, set()).add(new_id)
    return affected


def _changed_hop_prefixes(
    source: type[Model],
    paths: Iterable[str],
    owner_pks: dict[type[Model], set[Any]],
    through: type[Model] | None,
    changed_field: models.Field[Any, Any] | None,
) -> set[str]:
    from .field_backing import _relation_path

    prefixes: set[str] = set()

    def visit(owner: type[Model], field: Any, prefix: str) -> None:
        if owner._meta.concrete_model not in owner_pks:
            return
        if through is not None:
            hop_through = getattr(getattr(field, "remote_field", None), "through", None)
            hop_through = hop_through or getattr(field, "through", None)
            matches = (
                isinstance(hop_through, type)
                and issubclass(hop_through, models.Model)
                and hop_through._meta.concrete_model is through._meta.concrete_model
            )
        else:
            matches = field is changed_field or getattr(field, "field", None) is changed_field
        if matches:
            prefixes.add(prefix.rpartition("__")[0])

    for path in paths:
        _relation_path(source, path, lookup=True, visit=visit)
    return prefixes


def _edge_actor(instance: Any) -> tuple[SubjectRef | None, bool]:
    from .actors import current_actor, is_sudo
    from .errors import MissingActorError

    effective = getattr(instance, "effective_actor", None)
    if callable(effective):
        return cast(tuple[SubjectRef | None, bool], effective(strict=True))
    if is_sudo():
        return None, True
    actor = current_actor()
    if actor is None and app_settings.REBAC_STRICT_MODE:
        raise MissingActorError("Backed relation write requires an actor.")
    return actor, actor is None


def _check_edge_writes(
    actor: SubjectRef | None, resource_type: str, ids: set[str], *, bulk: bool = False
) -> None:
    from .backends import backend
    from .errors import PermissionDenied
    from .mixins import _maybe_audit_denial

    assert actor is not None
    allowed = set(backend().accessible(subject=actor, action="write", resource_type=resource_type))
    denied = ids - allowed
    if denied:
        resource = ObjectRef(resource_type, sorted(denied)[0])
        _maybe_audit_denial(actor=actor, action="write", resource=resource)
        if bulk:
            raise PermissionDenied("Bulk write: row outside actor scope.")
        raise PermissionDenied(f"Denied: {actor} cannot write {resource}")


def _gate_backed_rows(
    rows: Iterable[Model],
    *,
    using: str,
    names: Iterable[str] | None = None,
    proposed: dict[str, Any] | None = None,
    actor: SubjectRef | None = None,
    bypass: bool = False,
) -> None:
    """Batch the backed-edge gate for signal-free queryset writes."""
    from .errors import PermissionDenied

    if bypass:
        return
    candidates: dict[str, set[str]] = {}
    last_row: Model | None = None
    for row in rows:
        last_row = row
        if proposed is not None:
            from .index.maintain import current_pass, get_program

            outer = current_pass(using)
            program = outer.load_program() if outer is not None else get_program(using)
            watch = program.watched.get(row._meta.label_lower)
            for field in row._meta.concrete_fields:
                if watch is None or not {field.name, field.attname} & watch.fields:
                    continue
                key = field.name if field.name in proposed else field.attname
                if key not in proposed:
                    continue
                value = proposed[key]
                if callable(getattr(value, "resolve_expression", None)):
                    raise PermissionDenied(
                        "Bulk write cannot resolve a backed field expression; "
                        "use checked saves or sudo."
                    )
                if isinstance(value, Model):
                    setattr(row, field.name, value)
                else:
                    setattr(row, field.attname, value)
        for type_, ids in _backed_field_change_candidates(type(row), row, using, names).items():
            candidates.setdefault(type_, set()).update(ids)
    if not candidates:
        return
    if actor is None:
        # Tracked querysets have no pinned actor; use their ambient scope.
        assert last_row is not None
        actor, bypass = _edge_actor(last_row)
        if bypass:
            return
    for resource_type, ids in sorted(candidates.items()):
        _check_edge_writes(actor, resource_type, ids, bulk=True)


def _gate_backed_field_change(
    sender: type[Model], instance: Any, using: str, update_fields: Iterable[str] | None
) -> None:
    """Check old and new owners when a watched backing column changes."""
    candidates = _backed_field_change_candidates(sender, instance, using, update_fields)
    if not candidates:
        return
    actor, bypass = _edge_actor(instance)
    if bypass:
        return
    for resource_type, ids in sorted(candidates.items()):
        _check_edge_writes(actor, resource_type, ids)


def _backed_field_change_candidates(
    sender: type[Model], instance: Any, using: str, update_fields: Iterable[str] | None
) -> dict[str, set[str]]:
    from .index.maintain import current_pass, get_program

    if not _current_watch(sender, using, update_fields):
        return {}
    outer = current_pass(using)
    program = outer.load_program() if outer is not None else get_program(using)
    watch = program.watched.get(sender._meta.label_lower)
    if watch is None:
        return {}
    candidates: dict[str, set[str]] = {}
    changed_names = set(update_fields) if update_fields is not None else None
    for field in sender._meta.concrete_fields:
        if changed_names is not None and not {field.name, field.attname} & changed_names:
            continue
        if not {field.name, field.attname} & watch.fields:
            continue
        old = (
            sender._base_manager.using(using)
            .filter(pk=instance.pk)
            .values_list(field.attname, flat=True)
            .first()
            if not instance._state.adding
            else None
        )
        new = getattr(instance, field.attname)
        if old == new:
            continue
        sender_model = sender._meta.concrete_model
        assert sender_model is not None
        owner_pks: dict[type[Model], set[Any]] = {
            sender_model: {instance.pk} if instance.pk is not None else set(),
        }
        if isinstance(field, (models.ForeignKey, models.OneToOneField)):
            values = {value for value in (old, new) if value is not None}
            target_model = field.related_model._meta.concrete_model
            assert target_model is not None
            owner_pks.setdefault(target_model, set()).update(
                field.related_model._base_manager.using(using)
                .filter(**{f"{field.target_field.name}__in": values})
                .values_list("pk", flat=True)
            )
        for type_, ids in _affected_backing_ids(
            program,
            using=using,
            owner_pks=owner_pks,
            changed_field=field,
            proposed=instance,
            resource_types=set(watch.resource_types),
        ).items():
            candidates.setdefault(type_, set()).update(ids)
    return candidates


def _mark_schema_caches_stale() -> None:
    from .backends.local import mark_db_loaded_schemas_stale

    mark_db_loaded_schemas_stale()
