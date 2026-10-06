"""Explicit-sender lifecycle hooks for writes that bypass model/queryset owners."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from itertools import batched
from typing import Any, cast
from weakref import WeakSet

from django.apps import apps
from django.db import connections, models, router
from django.db.models import Model, Q
from django.db.models.functions import Cast
from django.db.models.signals import (
    class_prepared,
    m2m_changed,
    post_delete,
    pre_delete,
    pre_save,
)
from django.dispatch import receiver

from .actors import model_can_resolve_subject, to_subject_ref
from .conf import app_settings
from .errors import NoActorResolvedError, PermissionDenied
from .resources import model_resource_type, to_object_ref
from .types import ObjectRef, SubjectRef

# Weak sets retain no isolated app registry after its classes disappear.
_owned: WeakSet[type[Model]] = WeakSet()
_subjects: WeakSet[type[Model]] = WeakSet()
_tracked: WeakSet[type[Model]] = WeakSet()
_throughs: WeakSet[type[Model]] = WeakSet()
_through_candidates: WeakSet[type[Model]] = WeakSet()


class _PreGatedThroughWrite:
    """The through pairs a related-manager call already gated.

    Only these exact rows skip the per-row gates while the call runs; any other
    through row written during it, for example by a consumer ``m2m_changed``
    handler, is gated on its own.
    """

    __slots__ = ("pairs", "through")

    def __init__(self, through: type[Model], pairs: set[tuple[Any, Any]]) -> None:
        self.through = through
        self.pairs = frozenset(pairs)

    def covers(
        self,
        sender: type[Model],
        instance: Any,
        reverse: bool,
        pk_set: set[Any] | None,
        using: str,
    ) -> bool:
        if sender is not self.through:
            return False
        return _through_pairs(sender, instance, reverse, pk_set, using) <= self.pairs

    def covers_row(self, row: Model) -> bool:
        if type(row)._meta.concrete_model is not self.through._meta.concrete_model:
            return False
        fields = [
            field for field in self.through._meta.fields if isinstance(field, models.ForeignKey)
        ]
        return tuple(getattr(row, field.attname) for field in fields) in self.pairs


_related_m2m_write: ContextVar[_PreGatedThroughWrite | None] = ContextVar(
    "rebac_related_m2m_write", default=None
)


def _through_pairs(
    sender: type[Model],
    instance: Any,
    reverse: bool,
    pk_set: set[Any] | None,
    using: str,
) -> set[tuple[Any, Any]]:
    """The through rows ``(fk_a, fk_b)``, in field order, a signal is about."""
    return _through_edges(sender, instance, reverse, pk_set, using)[0]


def _through_edges(
    sender: type[Model],
    instance: Any,
    reverse: bool,
    pk_set: set[Any] | None,
    using: str,
) -> tuple[set[tuple[Any, Any]], set[Any]]:
    """The through pairs of a related-manager signal and the other-side ids."""
    fields = [field for field in sender._meta.fields if isinstance(field, models.ForeignKey)]
    instance_model = instance._meta.concrete_model
    assert instance_model is not None
    # A multi-table child writes through its parent's M2M, so match by lineage.
    own = [
        field
        for field in fields
        if (target := field.remote_field.model._meta.concrete_model) is not None
        and issubclass(instance_model, target)
    ]
    if not own:
        return set(), set()
    instance_field = fields[1 if reverse else 0] if len(own) == len(fields) else own[0]
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
    if instance_field is fields[0]:
        pairs = {(instance.pk, other) for other in other_ids}
    else:
        pairs = {(other, instance.pk) for other in other_ids}
    owner_meta = getattr(sender._meta.auto_created, "_meta", None)
    if owner_meta is not None and any(
        m2m.remote_field.through is sender and m2m.remote_field.symmetrical
        for m2m in cast(Any, owner_meta).local_many_to_many
    ):
        # Django writes the mirror row of a symmetrical self-M2M in the same call.
        pairs |= {(b, a) for a, b in pairs}
    return pairs, set(other_ids)


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
            _connect(pre_save, _tracked_pre_save, model)
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
            _connect(m2m_changed, _tracked_m2m, through)


def connect_tracked_signals() -> None:
    """Install the static sender superset from the registry only."""
    # Setting overrides may remove tracked classes; update only changed senders.
    for model in tuple(_tracked):
        if not tracked_model(model):
            for signal, handler in (
                (pre_save, _tracked_pre_save),
                (pre_delete, _rebac_pre_delete),
            ):
                signal.disconnect(sender=model, dispatch_uid=handler.__name__)
            _tracked.discard(model)
            if not model_can_resolve_subject(model):
                post_delete.disconnect(sender=model, dispatch_uid="_rebac_cascade_resource")
                _subjects.discard(model)
    for model in tuple(_throughs):
        if not tracked_through(model):
            m2m_changed.disconnect(sender=model, dispatch_uid="_tracked_m2m")
            _throughs.discard(model)
    for model in apps.get_models(include_auto_created=True):
        _register_model(model)
    _own_through_managers()
    _wrap_related_m2m_writes()


def _own_through_managers() -> None:
    from .managers import TrackedManager

    for through in tuple(_throughs):
        if getattr(through, "_rebac_owned_through_manager", False):
            continue
        through._meta.local_managers = [
            manager for manager in through._meta.local_managers if manager.name != "objects"
        ]
        through.add_to_class("objects", TrackedManager())
        cast(Any, through._meta)._expire_cache()
        cast(Any, through)._rebac_owned_through_manager = True


def _wrap_related_m2m_writes() -> None:
    """Preflight related-manager calls outside Django's internal atomic block."""
    for source in apps.get_models():
        for field in source._meta.local_many_to_many:
            through = field.remote_field.through
            if through not in _throughs:
                continue
            target = field.remote_field.model
            descriptors = [getattr(source, field.name)]
            accessor = field.remote_field.get_accessor_name()
            if accessor and hasattr(target, accessor):
                descriptors.append(getattr(target, accessor))
            for descriptor in descriptors:
                manager_class = descriptor.related_manager_cls
                if manager_class.__dict__.get("_rebac_edge_gate_wrapped"):
                    continue
                for operation in ("add", "remove", "clear", "set"):
                    original = getattr(manager_class, operation)

                    def guarded(
                        self: Any,
                        *args: Any,
                        _original: Any = original,
                        _operation: str = operation,
                        **kwargs: Any,
                    ) -> Any:
                        with audit_backed_denials():
                            gated_pairs: set[tuple[Any, Any]] = set()
                            if _operation in {"clear", "set"}:
                                gated_pairs |= _gate_m2m(
                                    self.through,
                                    self.instance,
                                    self.reverse,
                                    self.model,
                                    None,
                                    self.db,
                                )
                            if _operation != "clear":
                                values = (
                                    tuple(args[0] if args else kwargs["objs"])
                                    if _operation == "set"
                                    else args
                                )
                                ids = {getattr(value, "pk", value) for value in values}
                                gated_pairs |= _gate_m2m(
                                    self.through,
                                    self.instance,
                                    self.reverse,
                                    self.model,
                                    ids,
                                    self.db,
                                )
                                if _operation == "set":
                                    if args:
                                        args = (values, *args[1:])
                                    else:
                                        kwargs["objs"] = values
                            from .actors import actor_context
                            from .mixins import RebacMixin

                            pinned = getattr(self.instance, "actor", None)
                            carried = (
                                pinned()
                                if isinstance(self.instance, RebacMixin) and callable(pinned)
                                else None
                            )
                            scope = actor_context(carried) if carried is not None else nullcontext()
                            # Only the pairs gated above are exempt from the
                            # per-row gates below; a consumer m2m_changed
                            # handler writing other through rows during the
                            # call is gated like any other write.
                            token = _related_m2m_write.set(
                                _PreGatedThroughWrite(self.through, gated_pairs)
                            )
                            try:
                                with scope:
                                    return _original(self, *args, **kwargs)
                            finally:
                                _related_m2m_write.reset(token)

                    setattr(manager_class, operation, guarded)
                manager_class._rebac_edge_gate_wrapped = True


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
    from .models import active_relationship_model
    from .watch import gate_policy, model_is_watched

    if not router.allow_migrate_model(using, active_relationship_model()):
        return False
    policy = gate_policy(using)
    return policy is not None and model_is_watched(policy.watched, sender, names)


def _stored(model: type[Model], using: str) -> models.QuerySet[Any]:
    """The model's rows as a gate reads them before the write that follows.

    A gate decides on the stored values and the write then replaces them.
    Inside a transaction the rows are locked, so that no other writer changes
    them between the decision and the write.
    """
    rows = model._base_manager.using(using).order_by()
    connection = connections[using]
    if connection.features.has_select_for_update and connection.in_atomic_block:
        return rows.select_for_update()
    return rows


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
    """Remove the tuples that name deleted objects, at either end."""
    from django.db import transaction

    from .backends.local import mark_relationships_changed
    from .models import RebacResource, active_relationship_model

    identities = sorted(set(identities), key=str)
    if not identities or not router.allow_migrate_model(using, active_relationship_model()):
        return
    with transaction.atomic(using=using):
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


@receiver(post_delete, sender="rebac.SchemaOverride")
def _rebac_schema_cascade_revision(
    sender: type[Model], *, instance: Any, origin: Any = None, using: str, **_: Any
) -> None:
    """Cover collector cascades whose origin does not own schema publication."""
    from .models.generation import SchemaGeneration
    from .models.schema_write import SchemaQuerySet, SchemaRow, schema_index_write

    if not isinstance(origin, (SchemaRow, SchemaQuerySet)):
        with schema_index_write(using):
            SchemaGeneration.objects.advance(using=using)
            instance._audit_change(created=False)


@receiver(pre_delete, sender="rebac.SchemaOverride")
def _rebac_schema_cascade_begin(
    sender: type[Model], *, instance: Any, using: str, origin: Any = None, **_: Any
) -> None:
    from .models.schema_write import SchemaQuerySet, SchemaRow

    if not isinstance(origin, (SchemaRow, SchemaQuerySet)):
        # The target content type is gone after the delete; name it now.
        instance._audit_target = instance._audit_target_repr()


def _tracked_pre_save(
    sender: type[Model],
    instance: Any,
    using: str,
    raw: bool = False,
    update_fields: Iterable[str] | None = None,
    **kwargs: Any,
) -> None:
    if not raw:
        _gate_backed_field_change(sender, instance, using, update_fields)


def _tracked_m2m(
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
        pre_gated = _related_m2m_write.get()
        if pre_gated is None or not pre_gated.covers(sender, instance, reverse, pk_set, using):
            _gate_m2m(sender, instance, reverse, model, pk_set, using)


def _gate_m2m(
    sender: type[Model],
    instance: Any,
    reverse: bool,
    model: type[Model],
    pk_set: set[Any] | None,
    using: str,
) -> set[tuple[Any, Any]]:
    """Gate each resource endpoint owning an affected M2M-backed edge.

    Returns the through pairs the gate decided on, so the related-manager
    wrapper can exempt exactly those rows from the per-row gates; an early
    return yields no pairs, and the per-row gates then decide for themselves.
    """
    from .actors import is_sudo
    from .mixins import RebacMixin
    from .resources import model_for_resource_type
    from .watch import gate_policy

    pinned = getattr(instance, "actor", None)
    if is_sudo() and (
        not isinstance(instance, RebacMixin) or not callable(pinned) or pinned() is None
    ):
        return set()
    owner_model = sender._meta.auto_created
    if not isinstance(owner_model, type):
        return set()
    owner_type = model_resource_type(owner_model)
    program = gate_policy(using)
    if program is None:
        return set()
    watched = program.watched.get(sender._meta.label_lower)
    types = {owner_type} if owner_type is not None else set()
    if watched is not None:
        types.update(watched.resource_types)
    types = {type_ for type_ in types if program.has_node(type_, "write")}
    if not types or pk_set == set():
        return set()
    actor, bypass = _edge_actor(instance, relation=True)
    if bypass:
        return set()
    instance_model = instance._meta.concrete_model
    assert instance_model is not None
    pairs, other_ids = _through_edges(sender, instance, reverse, pk_set, using)
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
    return pairs


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
    for definition in program.schema.definitions:
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
                if backing.generic is not None:
                    paths = (*paths, backing.generic.ct_field)
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


def _edge_actor(instance: Any, *, relation: bool = False) -> tuple[SubjectRef | None, bool]:
    from .actors import current_actor, is_sudo
    from .errors import MissingActorError
    from .mixins import RebacMixin

    effective = getattr(instance, "effective_actor", None)
    if isinstance(instance, RebacMixin) and callable(effective) and not relation:
        return cast(tuple[SubjectRef | None, bool], effective(strict=True))
    if relation:
        pinned = getattr(instance, "actor", None)
        if (
            isinstance(instance, RebacMixin)
            and callable(pinned)
            and (actor := pinned()) is not None
        ):
            return cast(SubjectRef, actor), False
    if is_sudo():
        return None, True
    actor = current_actor()
    if actor is None and app_settings.REBAC_STRICT_MODE:
        raise MissingActorError("Backed relation write requires an actor.")
    return actor, actor is None


class BackedEdgeDenied(PermissionDenied):
    """Carry the denied declaring resource across an owner's rollback."""

    def __init__(self, message: str, actor: SubjectRef, resource: ObjectRef) -> None:
        super().__init__(message)
        self.actor = actor
        self.resource = resource


def audit_edge_denial(exc: BaseException) -> None:
    if isinstance(exc, BackedEdgeDenied):
        from .mixins import _maybe_audit_denial

        _maybe_audit_denial(actor=exc.actor, action="write", resource=exc.resource)


@contextmanager
def audit_backed_denials() -> Iterator[None]:
    try:
        yield
    except BackedEdgeDenied as exc:
        audit_edge_denial(exc)
        raise


def _check_edge_writes(
    actor: SubjectRef | None, resource_type: str, ids: set[str], *, bulk: bool = False
) -> None:
    from .backends import backend

    assert actor is not None
    active = backend()
    from .backends.local import LocalBackend

    if isinstance(active, LocalBackend):
        from .compile.read import held
        from .models import active_relationship_model

        allowed = held(
            backend=active,
            resource_type=resource_type,
            action="write",
            actor=actor,
            ids=ids,
            using=active_relationship_model().objects.db,
        )
    else:
        allowed = {
            id_
            for id_ in ids
            if active.check_access(
                subject=actor, action="write", resource=ObjectRef(resource_type, id_)
            ).allowed
        }
    denied = ids - allowed
    if denied:
        resource = ObjectRef(resource_type, sorted(denied)[0])
        if bulk:
            raise BackedEdgeDenied("Bulk write: row outside actor scope.", actor, resource)
        raise BackedEdgeDenied(f"Denied: {actor} cannot write {resource}", actor, resource)


def _gate_backed_rows(
    rows: Iterable[Model],
    *,
    using: str,
    names: Iterable[str] | None = None,
    proposed: dict[str, Any] | None = None,
    actor: SubjectRef | None = None,
    bypass: bool = False,
    deleting: bool = False,
) -> None:
    """Batch the backed-edge gate for signal-free queryset writes."""
    from .errors import PermissionDenied

    if bypass:
        return
    changed_names = set(names) if names is not None else None
    materialized = list(rows)
    pre_gated = _related_m2m_write.get()
    if pre_gated is not None and materialized and proposed is None:
        # The related-manager wrapper checked exactly these pairs as inserts
        # or deletes; rows a consumer handler adds during the call, and any
        # update that would move a gated pair elsewhere, are still gated here.
        materialized = [row for row in materialized if not pre_gated.covers_row(row)]
        if not materialized:
            return
    from .watch import gate_policy

    program = gate_policy(using)
    if program is None:
        return
    stored: dict[tuple[type[Model], Any], dict[str, Any]] = {}
    model_pks: dict[type[Model], set[Any]] = {}
    for row in materialized:
        if row.pk is not None and not row._state.adding:
            model_pks.setdefault(type(row), set()).add(row.pk)
    for model, pks in model_pks.items():
        if not _current_watch(model, using, changed_names):
            continue
        watch = program.watched.get(model._meta.label_lower)
        if watch is None:
            continue
        columns = sorted(
            {
                field.attname
                for field in model._meta.concrete_fields
                if {field.name, field.attname} & watch.fields
                and (changed_names is None or {field.name, field.attname} & changed_names)
            }
            - {model._meta.pk.attname}
        )
        if not columns:
            continue
        for values in _stored(model, using).filter(pk__in=pks).values("pk", *columns):
            stored[(model, values["pk"])] = values
    through_pairs: dict[type[Model], set[tuple[Any, Any]]] = {}
    last_row: Model | None = None
    for row in materialized:
        last_row = row
        if row._meta.auto_created:
            _collect_through_pair(row, through_pairs)
        if proposed is not None:
            watch = program.watched.get(row._meta.label_lower)
            for field in row._meta.concrete_fields:
                if watch is None or not {field.name, field.attname} & watch.fields:
                    continue
                key = field.name if field.name in proposed else field.attname
                if key not in proposed:
                    continue
                value = proposed[key]
                if isinstance(value, Cast):
                    value = value.source_expressions[0]
                if isinstance(value, models.Case):
                    # Only ``bulk_update``'s shape resolves: every arm is a
                    # ``When(pk=<literal>, then=Value(...))``. Any other arm
                    # (a non-pk condition, an expression result) could shadow
                    # a row's value in SQL, so the whole Case is refused.
                    literal_arms = [
                        when
                        for when in value.cases
                        if isinstance(when, models.When)
                        and isinstance(when.result, models.Value)
                        and len(when.condition.children) == 1
                        and isinstance(when.condition.children[0], tuple)
                        and when.condition.children[0][0] == "pk"
                    ]
                    if len(literal_arms) != len(value.cases):
                        raise PermissionDenied(
                            "Bulk write cannot resolve a backed field expression; "
                            "use checked saves or sudo."
                        )
                    matched = [
                        when.result.value
                        for when in literal_arms
                        if when.condition.children[0] == ("pk", row.pk)
                    ]
                    # The rows here are the statement's own (the owner froze
                    # them before the SQL), so every one of them is written:
                    # a row without exactly one ``When(pk=...)`` literal, or a
                    # ``Case`` whose default is not NULL (``bulk_update`` sets
                    # none), cannot be resolved and is refused.
                    default = value.default
                    if (
                        not (isinstance(default, models.Value) and default.value is None)
                        or len(matched) != 1
                    ):
                        raise PermissionDenied(
                            "Bulk write cannot resolve a backed field expression; "
                            "use checked saves or sudo."
                        )
                    value = matched[0]
                if callable(getattr(value, "resolve_expression", None)):
                    raise PermissionDenied(
                        "Bulk write cannot resolve a backed field expression; "
                        "use checked saves or sudo."
                    )
                if isinstance(value, Model):
                    setattr(row, field.name, value)
                else:
                    setattr(row, field.attname, value)
            if row._meta.auto_created:
                _collect_through_pair(row, through_pairs)
    candidates = _batched_backed_field_candidates(
        materialized, stored, program, using, changed_names, deleting=deleting
    )
    _gate_direct_through_rows(through_pairs, using)
    if not candidates:
        return
    candidates = {
        type_: ids for type_, ids in candidates.items() if program.has_node(type_, "write")
    }
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


def _batched_backed_field_candidates(
    rows: list[Model],
    stored: dict[tuple[type[Model], Any], dict[str, Any]],
    program: Any,
    using: str,
    changed_names: set[str] | None,
    *,
    deleting: bool,
) -> dict[str, set[str]]:
    """Resolve each watched FK and reverse source once for a queryset write."""
    from .field_backing import resolve_attribute_backing

    groups: dict[
        tuple[type[Model], models.Field[Any, Any]],
        tuple[dict[type[Model], set[Any]], set[Any], list[Model], set[str]],
    ] = {}
    for row in rows:
        model = type(row)
        watch = program.watched.get(model._meta.label_lower)
        if watch is None:
            continue
        old_values = stored.get((model, row.pk), {})
        for field in model._meta.concrete_fields:
            if changed_names is not None and not {field.name, field.attname} & changed_names:
                continue
            if not {field.name, field.attname} & watch.fields:
                continue
            old = old_values.get(field.attname)
            new = None if deleting else getattr(row, field.attname)
            if old == new:
                continue
            concrete = model._meta.concrete_model
            assert concrete is not None
            owner_pks, fk_values, proposed_rows, resource_types = groups.setdefault(
                (model, field), ({}, set(), [], set(watch.resource_types))
            )
            owner_pks.setdefault(concrete, set())
            if row.pk is not None and not row._state.adding:
                owner_pks[concrete].add(row.pk)
            if isinstance(field, (models.ForeignKey, models.OneToOneField)):
                fk_values.update(value for value in (old, new) if value is not None)
            if not deleting:
                proposed_rows.append(row)
    candidates: dict[str, set[str]] = {}
    for (model, field), (owner_pks, fk_values, proposed_rows, resource_types) in groups.items():
        if fk_values and isinstance(field, (models.ForeignKey, models.OneToOneField)):
            target_model = field.related_model._meta.concrete_model
            assert target_model is not None
            owner_pks.setdefault(target_model, set()).update(
                field.related_model._base_manager.using(using)
                .filter(**{f"{field.target_field.name}__in": fk_values})
                .values_list("pk", flat=True)
            )
        for type_, ids in _affected_backing_ids(
            program,
            using=using,
            owner_pks=owner_pks,
            changed_field=field,
            resource_types=resource_types,
        ).items():
            candidates.setdefault(type_, set()).update(ids)
        for definition in program.schema.definitions:
            if definition.resource_type not in resource_types:
                continue
            for relation in definition.relations:
                attribute = resolve_attribute_backing(definition, relation)
                if (
                    attribute is None
                    or attribute.field is not field
                    or attribute.target_model._meta.concrete_model is not model._meta.concrete_model
                ):
                    continue
                for row in proposed_rows:
                    value = getattr(row, field.attname)
                    new_id = (
                        attribute.container_id_of(value)
                        if attribute.resource is None
                        else attribute.resource
                        if value == attribute.value
                        else None
                    )
                    if new_id is not None:
                        candidates.setdefault(definition.resource_type, set()).add(new_id)
    return candidates


def _collect_through_pair(row: Model, pairs: dict[type[Model], set[tuple[Any, Any]]]) -> None:
    owner_model = row._meta.auto_created
    if not isinstance(owner_model, type):
        return
    fields = [field for field in row._meta.fields if isinstance(field, models.ForeignKey)]
    source_field = next(
        (field for field in fields if field.remote_field.model is owner_model), None
    )
    if source_field is None:
        return
    target_field = next(field for field in fields if field is not source_field)
    source_pk = getattr(row, source_field.attname)
    target_pk = getattr(row, target_field.attname)
    if source_pk is None or target_pk is None:
        return
    pairs.setdefault(type(row), set()).add((source_pk, target_pk))


def _gate_direct_through_rows(pairs: dict[type[Model], set[tuple[Any, Any]]], using: str) -> None:
    from .resources import model_for_resource_type
    from .watch import gate_policy

    program = gate_policy(using)
    if program is None:
        return
    for through, changed in pairs.items():
        owner_model = through._meta.auto_created
        if not isinstance(owner_model, type):
            continue
        fields = [field for field in through._meta.fields if isinstance(field, models.ForeignKey)]
        source_field = next(
            (field for field in fields if field.remote_field.model is owner_model), None
        )
        if source_field is None:
            continue
        target_field = next(field for field in fields if field is not source_field)
        owner_type = model_resource_type(owner_model)
        watched = program.watched.get(through._meta.label_lower)
        types = {owner_type} if owner_type is not None else set()
        if watched is not None:
            types.update(watched.resource_types)
        types = {type_ for type_ in types if program.has_node(type_, "write")}
        if not types:
            continue
        source_ids = {source_pk for source_pk, _ in changed}
        target_ids = {target_pk for _, target_pk in changed}
        sources = list(owner_model._base_manager.using(using).filter(pk__in=source_ids))
        if not sources:
            continue
        actor, bypass = _edge_actor(sources[0], relation=True)
        if bypass:
            continue
        target_model = target_field.remote_field.model
        source_concrete = owner_model._meta.concrete_model
        target_concrete = target_model._meta.concrete_model
        assert source_concrete is not None and target_concrete is not None
        owner_pks: dict[type[Model], set[Any]] = {source_concrete: {row.pk for row in sources}}
        owner_pks.setdefault(target_concrete, set()).update(target_ids)
        backing_ids = _affected_backing_ids(
            program, using=using, owner_pks=owner_pks, through=through, resource_types=types
        )
        for resource_type in sorted(types):
            ids = backing_ids.get(resource_type, set())
            resource_model = model_for_resource_type(resource_type)
            if resource_model is not None:
                concrete = resource_model._meta.concrete_model
                if concrete is source_concrete:
                    ids.update(to_object_ref(row).resource_id for row in sources)
                if concrete is target_concrete:
                    ids.update(
                        to_object_ref(row).resource_id
                        for row in resource_model._base_manager.using(using).filter(
                            pk__in=target_ids
                        )
                    )
            if ids:
                _check_edge_writes(actor, resource_type, ids, bulk=True)


def _gate_backed_field_change(
    sender: type[Model],
    instance: Any,
    using: str,
    update_fields: Iterable[str] | None,
    *,
    deleting: bool = False,
) -> None:
    """Check old and new owners when a watched backing column changes."""
    candidates = _backed_field_change_candidates(
        sender, instance, using, update_fields, deleting=deleting
    )
    if not candidates:
        return
    from .watch import gate_policy

    program = gate_policy(using)
    if program is None:
        return
    candidates = {
        type_: ids for type_, ids in candidates.items() if program.has_node(type_, "write")
    }
    if not candidates:
        return
    actor, bypass = _edge_actor(instance)
    if bypass:
        return
    for resource_type, ids in sorted(candidates.items()):
        _check_edge_writes(actor, resource_type, ids)


def _backed_field_change_candidates(
    sender: type[Model],
    instance: Any,
    using: str,
    update_fields: Iterable[str] | None,
    *,
    deleting: bool = False,
    stored: dict[str, Any] | None = None,
) -> dict[str, set[str]]:
    from .watch import gate_policy

    if not _current_watch(sender, using, update_fields):
        return {}
    program = gate_policy(using)
    if program is None:
        return {}
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
            stored.get(field.attname)
            if stored is not None
            else (
                _stored(sender, using)
                .filter(pk=instance.pk)
                .values_list(field.attname, flat=True)
                .first()
                if not instance._state.adding
                else None
            )
        )
        new = None if deleting else getattr(instance, field.attname)
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
            proposed=None if deleting else instance,
            resource_types=set(watch.resource_types),
        ).items():
            candidates.setdefault(type_, set()).update(ids)
    return candidates


def _mark_schema_caches_stale() -> None:
    from .backends.local import mark_db_loaded_schemas_stale

    mark_db_loaded_schemas_stale()
