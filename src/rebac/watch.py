"""Which model columns a schema's backings read, and the owners of gated writes.

Nothing here keeps state about application rows.  The watch map says which
models and columns carry permission structure, so that a write to one of them
can be gated; ``model_write`` is the one transaction such a write runs in.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from itertools import batched
from threading import Lock
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from django.core.exceptions import FieldDoesNotExist
from django.db import DatabaseError, connections, models, transaction

from ._id import model_identity_fields, resource_id_attr
from .field_backing import (
    _relation_path,
    resolve_attribute_backing,
    resolve_const_backing,
    resolve_field_backing,
)
from .resources import model_for_resource_type, model_for_subject_type
from .schema.ast import AttributeBinding, FieldBinding, Schema

if TYPE_CHECKING:
    from .backends.local import LocalBackend


@dataclass(frozen=True)
class WatchSpec:
    model_label: str
    fields: frozenset[str]
    resource_types: frozenset[str]
    is_mixin: bool
    model: type[models.Model]


class _Watches:
    def __init__(self) -> None:
        self.fields: dict[str, set[str]] = {}
        self.types: dict[str, set[str]] = {}
        self.models: dict[str, type[models.Model]] = {}

    def model(self, model: type[models.Model], resource_type: str) -> None:
        label = model._meta.label_lower
        self.models[label] = model
        self.fields.setdefault(label, set())
        self.types.setdefault(label, set()).add(resource_type)
        pk = model._meta.pk
        if pk is not None:
            self.field(model, pk, resource_type)
        for parent, link in sorted(
            model._meta.parents.items(), key=lambda pair: pair[0]._meta.label_lower
        ):
            self.model(parent, resource_type)
            if link is not None:
                self.field(model, link, resource_type)

    def field(self, model: type[models.Model], field: Any, resource_type: str) -> None:
        for owner in (model, getattr(field, "model", model)):
            label = owner._meta.label_lower
            self.models[label] = owner
            self.types.setdefault(label, set()).add(resource_type)
            names = self.fields.setdefault(label, set())
            names.update(
                value
                for attr in ("name", "attname", "column")
                if (value := getattr(field, attr, None))
            )

    def identity(self, model: type[models.Model], attr: str, resource_type: str) -> None:
        self.model(model, resource_type)
        try:
            field, _ = model_identity_fields(model, attr)
        except ValueError, FieldDoesNotExist:
            return  # E014 reports the unsupported identity separately.
        self.field(model, field, resource_type)

    def path(self, model: type[models.Model], path: str, resource_type: str) -> None:
        self.model(model, resource_type)

        def visit(model: type[models.Model], field: Any, prefix: str) -> None:
            self.field(model, field, resource_type)
            if not field.is_relation:
                return
            # Django resolves forward, reverse, M2M, and MTI joins here, including
            # the through model and to_field columns. No parallel path resolver.
            for info in getattr(field, "path_infos", ()):
                for options in (info.from_opts, info.to_opts):
                    self.model(options.model, resource_type)
                join = info.join_field
                forward = getattr(join, "field", join)
                for left, right in getattr(forward, "related_fields", ()):
                    self.field(left.model, left, resource_type)
                    self.field(right.model, right, resource_type)
            target = getattr(field, "related_model", None)
            if not isinstance(target, type) or not issubclass(target, models.Model):
                return
            # GenericRelation's object-id join also depends on content_type.
            for attr in ("object_id_field_name", "content_type_field_name"):
                name = getattr(field, attr, None)
                if name:
                    self.field(target, target._meta.get_field(name), resource_type)
            self.model(target, resource_type)

        _relation_path(model, path, lookup=True, visit=visit)

    def freeze(self) -> Mapping[str, WatchSpec]:
        from .mixins import RebacTrackedMixin

        return MappingProxyType(
            {
                label: WatchSpec(
                    label,
                    frozenset(self.fields[label]),
                    frozenset(self.types[label]),
                    issubclass(self.models[label], RebacTrackedMixin),
                    self.models[label],
                )
                for label in sorted(self.models)
            }
        )


def watched_for(schema: Schema) -> Mapping[str, WatchSpec]:
    """Resolve every backing's mutable columns without querying model rows."""
    watches = _Watches()
    for definition in sorted(schema.definitions, key=lambda d: d.resource_type):
        type_ = definition.resource_type
        for relation in sorted(definition.relations, key=lambda r: r.name):
            field = resolve_field_backing(definition, relation)
            if field is not None:
                watches.identity(field.source_model, field.source_id_attr, type_)
                watches.identity(field.target_model, field.target_id_attr, type_)
                watches.path(field.source_model, field.path, type_)
                for lookup in sorted(field.filters):
                    watches.path(field.source_model, lookup, type_)
            attribute = resolve_attribute_backing(definition, relation)
            if attribute is not None:
                watches.identity(attribute.target_model, attribute.target_id_attr, type_)
                watches.path(attribute.target_model, attribute.field.name, type_)
                for lookup in sorted(attribute.filters):
                    watches.path(attribute.target_model, lookup, type_)
            const = resolve_const_backing(definition, relation)
            if const is not None:
                watches.identity(const.source_model, resource_id_attr(const.source_model), type_)
                for lookup in sorted(const.filters):
                    watches.path(const.source_model, lookup, type_)
    return watches.freeze()


def codec_fields(schema: Schema) -> tuple[tuple[type[models.Model], str], ...]:
    """Identity/attribute columns requiring codecs, excluding fixed anchors."""
    result: dict[tuple[str, str], tuple[type[models.Model], str]] = {}

    def add(model: type[models.Model], attr: str) -> None:
        result[model._meta.label_lower, attr] = (model, attr)

    for definition in sorted(schema.definitions, key=lambda d: d.resource_type):
        model = model_for_resource_type(definition.resource_type)
        if model is not None:
            add(model, resource_id_attr(model))
        for relation in definition.relations:
            if isinstance(relation.backing, (FieldBinding, AttributeBinding)):
                for allowed in relation.allowed_subjects:
                    target = model_for_subject_type(allowed.type)
                    if target is not None:
                        add(*target)
            field = resolve_field_backing(definition, relation)
            if field is not None:
                add(field.source_model, field.source_id_attr)
                add(field.target_model, field.target_id_attr)
            attribute = resolve_attribute_backing(definition, relation)
            if attribute is not None:
                add(attribute.target_model, attribute.target_id_attr)
                if (
                    isinstance(relation.backing, AttributeBinding)
                    and relation.backing.resource is None
                ):
                    add(attribute.target_model, attribute.field.name)
    return tuple(result[key] for key in sorted(result))


def model_lineage(model: type[models.Model]) -> tuple[type[models.Model], ...]:
    """Include proxy targets and every concrete ancestor of an MTI write."""
    concrete = model._meta.concrete_model
    assert concrete is not None
    return tuple(
        dict.fromkeys(
            (model, concrete, *(parent for parent in concrete._meta.all_parents if parent))
        )
    )


def model_is_watched(
    watched: Mapping[str, WatchSpec], model: type[models.Model], names: Iterable[str] | None = None
) -> bool:
    specs = [
        watch
        for candidate in model_lineage(model)
        if (watch := watched.get(candidate._meta.label_lower)) is not None
        and watch.model is candidate
    ]
    if names is None:
        return bool(specs)
    normalized = set(names)
    for field in model._meta.concrete_fields:
        if field.name in normalized or field.attname in normalized:
            normalized.update((field.name, field.attname))
    return any(normalized & watch.fields for watch in specs)


@dataclass(frozen=True)
class GatePolicy:
    """What the write gates need of the installed policy."""

    schema: Schema
    watched: Mapping[str, WatchSpec]

    def has_node(self, resource_type: str, name: str) -> bool:
        definition = self.schema.get_definition(resource_type)
        return definition is not None and (
            any(permission.name == name for permission in definition.permissions)
            or any(relation.name == name for relation in definition.relations)
        )


_lock = Lock()
_policies: OrderedDict[int, tuple[Schema, GatePolicy]] = OrderedDict()
# The gate policy of each alias an outermost ``model_write`` is open on.
_open: ContextVar[Mapping[str, GatePolicy | None]] = ContextVar(
    "rebac_model_writes", default=MappingProxyType({})
)


def reset() -> None:
    with _lock:
        _policies.clear()


def installed(using: str, backend: LocalBackend) -> bool:
    """Whether a policy is installed on the alias, so that there are writes to gate.

    Before the first ``sync`` there is none: a write proceeds and reads stay
    closed.  While migrations run, the library's own tables can be missing or
    lack a column; the revision cannot be read then, and the answer is the
    same.  Inside a transaction the read has its own savepoint, so a failure
    leaves the caller's transaction usable.
    """
    from .models.generation import SchemaGeneration

    if backend._schema_is_manual:
        return True
    guard = transaction.atomic(using=using) if connections[using].in_atomic_block else nullcontext()
    try:
        with guard:
            revision = SchemaGeneration.objects.revision(using)
    except DatabaseError:
        return False
    return bool(revision)


def gate_policy(using: str, backend: LocalBackend | None = None) -> GatePolicy | None:
    """The schema and watch map to gate writes on ``using`` with, if any.

    Inside a ``model_write`` it is the policy that write started with: one
    write is gated by one policy, however many rows and owners it nests.
    """
    from .backends import backend as active_backend
    from .backends.local import LocalBackend

    if backend is None:
        held = _open.get()
        if using in held:
            return held[using]
    active = backend if backend is not None else active_backend()
    if not isinstance(active, LocalBackend) or not installed(using, active):
        return None
    schema = active.schema()
    with _lock:
        kept = _policies.get(id(schema))
        if kept is not None and kept[0] is schema:
            _policies.move_to_end(id(schema))
            return kept[1]
    policy = GatePolicy(schema, watched_for(schema))
    with _lock:
        _policies[id(schema)] = schema, policy
        while len(_policies) > 64:
            _policies.popitem(last=False)
    return policy


@contextmanager
def model_write(
    *, model: type[models.Model], using: str, names: Iterable[str] | None = None
) -> Iterator[bool]:
    """One transaction around a source write; yields whether it must be gated.

    The value is true when the installed policy reads a column this write can
    change.  Nothing is maintained afterwards: reads see the columns as they
    are.
    """
    held = _open.get()
    policy = gate_policy(using)
    token = _open.set({**held, using: policy}) if using not in held else None
    try:
        with transaction.atomic(using=using):
            yield policy is not None and model_is_watched(policy.watched, model, names)
    finally:
        if token is not None:
            _open.reset(token)


KEY_CHUNK = 5000


def statement_keys(queryset: models.QuerySet[Any]) -> list[Any]:
    """The primary keys of the rows a queryset write is about to change.

    A gate decides on exactly these rows, reading their stored values under a
    row lock, and the write changes exactly these rows.  A row that starts
    matching the caller's filter in between is neither gated nor written.
    """
    return list(queryset.order_by().values_list("pk", flat=True))


def by_key[Rows: models.QuerySet[Any]](rows: Rows, keys: Sequence[Any]) -> Iterator[Rows]:
    """``rows`` restricted to ``keys``, in parts that fit one statement each."""
    for chunk in batched(keys, KEY_CHUNK, strict=False):
        yield rows.filter(pk__in=chunk)
    if not keys:
        yield rows.filter(pk__in=())
