"""Resolve schema-declared live ORM and constant relations."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, Any

from django.core.exceptions import FieldDoesNotExist, FieldError, ValidationError
from django.db import DEFAULT_DB_ALIAS, connections, models, router
from django.db.models import Q, QuerySet, Value
from django.db.models.expressions import BaseExpression, Col, ColPairs, Combinable
from django.db.models.functions import Coalesce
from django.db.models.sql import Query
from django.db.models.sql.constants import SINGLE

from ._candidate_filters import _candidate_literal, _filter_candidate_targets, _value_is_unresolved
from ._id import model_identity_fields, model_identity_filter, resource_id_attr
from .resources import (
    _resolve_dotted,
    model_for_resource_type,
    model_for_subject_type,
    model_resource_type,
)
from .schema.ast import (
    AttributeBinding,
    ConstBinding,
    Definition,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    Relation,
    Schema,
)
from .types import ObjectRef, SubjectRef

if TYPE_CHECKING:
    from django.contrib.contenttypes.fields import GenericForeignKey
    from django.contrib.contenttypes.models import ContentType

    # ``models.Field`` is generic only in django-stubs; subscripting it at
    # runtime raises ``TypeError``. Annotations are lazy (PEP 563), so the alias
    # is needed by type checkers only.
    from django.db.models.fields.reverse_related import ForeignObjectRel

    ModelField = models.Field[Any, Any] | ForeignObjectRel


class _SourceFilters:
    """Source-column predicates shared by field and constant backings."""

    __slots__ = ()
    relation: Relation

    @property
    def filters(self) -> dict[str, Any]:
        backing = self.relation.backing
        return dict(backing.filters) if isinstance(backing, (FieldBinding, ConstBinding)) else {}


@dataclass(frozen=True, slots=True)
class GenericColumns:
    """The names of a GenericForeignKey and of its two columns."""

    name: str
    ct_field: str
    fk_field: str


@dataclass(frozen=True, slots=True)
class ResolvedFieldBacking(_SourceFilters):
    """A forward/reverse ORM path with filters anchored on its source model."""

    source_model: type[models.Model]
    target_model: type[models.Model]
    field: ModelField
    relation: Relation
    target_resource_type: str
    target_id_attr: str
    path: str
    # For a backing that ends in a GenericForeignKey: ``path`` is its object
    # id column, which holds the target's primary key, and the content type
    # column selects the rows of ``target_model``.
    generic: GenericColumns | None = None

    @property
    def source_id_attr(self) -> str:
        return resource_id_attr(self.source_model)

    def generic_q(self, using: str, *, exists: bool = True) -> Q:
        """The rows of a GenericForeignKey backing that name a row of the target model.

        ``exists`` also requires the named row to be there: nothing
        constrains the object id, so an edge can name a row that is gone.
        Empty for any other backing.
        """
        if self.generic is None:
            return Q()
        content_type = _content_type(self.target_model, using)
        if content_type is None:
            # No row can carry a content type the database does not have.
            return Q(pk__in=[])
        predicate = Q(**{self.generic.ct_field: content_type})
        if exists:
            rows = self.target_model._base_manager.using(using).order_by().values("pk")
            predicate &= Q(**{f"{self.path}__in": rows})
        return predicate

    def source_filter(self, resource_id: str, *, using: str = DEFAULT_DB_ALIAS) -> Q:
        return model_identity_filter(
            self.source_model, self.source_id_attr, resource_id, using=using
        )

    def target_filter(self, subject: SubjectRef) -> dict[str, str]:
        return {self.target_values_path(): subject.subject_id}

    def target_in_filter(self, resource_ids: Iterable[str]) -> dict[str, Iterable[str]]:
        return {f"{self.target_values_path()}__in": resource_ids}

    def source_values_path(self) -> str:
        return self.source_id_attr

    def targets_identity_directly(self) -> bool:
        """Whether a forward FK column stores the target's REBAC identity."""

        if not isinstance(self.field, (models.ForeignKey, models.OneToOneField)):
            return False
        target_identity = (
            self.target_model._meta.pk
            if self.target_id_attr == "pk"
            else self.target_model._meta.get_field(self.target_id_attr)
        )
        return self.field.target_field is target_identity

    def keeps_target(self, using: str) -> bool:
        """Whether a stored reference to a target proves that the target's row exists.

        Django reads a column of the target from the nearest table that holds
        its value and leaves the others unjoined, so the path proves the row
        only when it ends in a forward foreign key and the database keeps
        every forward foreign key on it.  A reverse foreign key on the way is
        read from the table that holds it; a many-to-many hop is not relied
        on.
        """

        if not isinstance(self.field, (models.ForeignKey, models.OneToOneField)):
            return False
        kept = True

        def hop(_model: type[models.Model], field: ModelField, _prefix: str) -> None:
            nonlocal kept
            if isinstance(field, (models.ForeignKey, models.OneToOneField)):
                kept = kept and foreign_key_kept(field, using)
            elif not isinstance(field, (models.ManyToOneRel, models.OneToOneRel)):
                kept = False

        _relation_path(self.source_model, self.path, visit=hop)
        return kept

    def target_values_path(self) -> str:
        if self.generic is not None:
            return self.path
        if (
            "__" not in self.path
            and isinstance(self.field, (models.ForeignKey, models.OneToOneField))
            and self.targets_identity_directly()
        ):
            return self.field.attname
        return f"{self.path}__{self.target_id_attr}"

    def queryset(
        self,
        *,
        resource_id: str | None = None,
        subject: SubjectRef | None = None,
        target_ids: Iterable[str] | None = None,
        using: str | None = None,
    ) -> QuerySet[Any]:
        """Constrain the target and through-row predicates in the same SQL join."""

        rows = self.source_model._base_manager.db_manager(using).all()
        predicate = Q(**self.filters) & self.generic_q(rows.db)
        if resource_id is not None:
            predicate &= self.source_filter(resource_id, using=rows.db)
        if subject is not None:
            predicate &= Q(**self.target_filter(subject))
        if target_ids is not None:
            predicate &= Q(**self.target_in_filter(target_ids))
        return rows.filter(predicate)


def canonical_model(model: type[models.Model]) -> type[models.Model] | None:
    """The model a row of ``model`` is named by in a polymorphic edge, if any.

    A proxy is its concrete model.  Along the chain of parent links that are
    the primary key (a multi-table child shares its parent's key), the
    topmost model with a resource type is the canonical one: a child and its
    typed parent share one set of edges.  ``None`` when no model of the chain
    has a resource type.
    """
    current = model._meta.concrete_model or model
    found = current if model_resource_type(current) else None
    while True:
        pk = current._meta.pk
        if not (isinstance(pk, models.OneToOneField) and pk.remote_field.parent_link):
            return found
        parent = pk.related_model
        assert isinstance(parent, type)
        current = parent._meta.concrete_model or parent
        if model_resource_type(current):
            found = current


@dataclass(frozen=True, slots=True)
class GenericTarget:
    """What a polymorphic edge stores for a row, and the object it names."""

    content_type: ContentType
    object_id: Any
    ref: ObjectRef

    def lookups(self, model: type[models.Model], name: str) -> dict[str, Any]:
        """Filter keywords for the edges of ``model`` whose GenericForeignKey ``name`` names it."""
        from django.contrib.contenttypes.fields import GenericForeignKey

        field = model._meta.get_field(name)
        if not isinstance(field, GenericForeignKey):
            raise ValueError(f"{model.__name__}.{name} is not a GenericForeignKey")
        return {field.ct_field: self.content_type, field.fk_field: self.object_id}


def generic_target(obj: models.Model, *, using: str | None = None) -> GenericTarget:
    """The content type and id a polymorphic edge stores for ``obj``, and its object.

    The content type is that of the row's canonical model
    (:func:`canonical_model`): a proxy is stored as its concrete row, a
    multi-table child as its topmost typed ancestor.  Raises ``ValueError``
    for a row whose model chain has no resource type.
    """
    from django.contrib.contenttypes.models import ContentType

    model = canonical_model(type(obj))
    resource_type = model_resource_type(model) if model is not None else None
    if model is None or resource_type is None:
        raise ValueError(
            f"{type(obj).__name__} has no resource type: a polymorphic edge cannot name it"
        )
    if obj.pk is None:
        raise ValueError(f"An unsaved {type(obj).__name__} has no primary key to name")
    alias = using or obj._state.db or router.db_for_read(model)
    content_type = ContentType.objects.db_manager(alias).get_for_model(model)
    identity = _resolve_dotted(obj, resource_id_attr(model))
    return GenericTarget(content_type, obj.pk, ObjectRef(resource_type, str(identity)))


def _content_type(model: type[models.Model], using: str) -> ContentType | None:
    """The content type of ``model`` on ``using``, read and never created."""
    from django.contrib.contenttypes.models import ContentType

    name = model._meta.model_name
    assert name is not None
    try:
        return ContentType.objects.db_manager(using).get_by_natural_key(model._meta.app_label, name)
    except ContentType.DoesNotExist:
        return None


def _generic_field(model: type[models.Model], path: str) -> GenericForeignKey | None:
    from django.contrib.contenttypes.fields import GenericForeignKey

    if "__" in path:
        return None
    try:
        field = model._meta.get_field(path)
    except FieldDoesNotExist:
        return None
    return field if isinstance(field, GenericForeignKey) else None


# Column types that hold the same values, whatever their width.
_STRING_TYPES = frozenset({"CharField", "TextField", "SlugField"})
_INTEGER_TYPES = frozenset(
    {
        "AutoField",
        "BigAutoField",
        "SmallAutoField",
        "IntegerField",
        "BigIntegerField",
        "SmallIntegerField",
        "PositiveIntegerField",
        "PositiveBigIntegerField",
        "PositiveSmallIntegerField",
    }
)


def _resolve_generic_backing(
    definition: Definition,
    relation: Relation,
    source_model: type[models.Model],
    generic: GenericForeignKey,
    target_model: type[models.Model],
    target_id_attr: str,
) -> ResolvedFieldBacking:
    """A relation over one type, read from a GenericForeignKey of the declaring model."""
    allowed = relation.allowed_subjects[0]
    name = f"GenericForeignKey {source_model.__name__}.{generic.name}"
    if allowed.relation:
        raise ValueError(f"{name} names rows; it cannot back a subject relation")
    if allowed.wildcard:
        raise ValueError(f"{name} names rows; it cannot admit a wildcard")
    canonical = canonical_model(target_model)
    if canonical is not target_model:
        raise ValueError(
            f"{name} cannot back {allowed.type!r}: its model is not its own canonical model, "
            f"so edges to its rows are stored as {getattr(canonical, '__name__', None)!r}"
        )
    pk = target_model._meta.pk
    assert pk is not None
    if target_id_attr not in ("pk", pk.name, pk.attname):
        raise ValueError(
            f"{name} cannot back {allowed.type!r}: the object id holds a primary key, "
            f"and the type's identity {target_id_attr!r} is not its primary key"
        )
    object_id = source_model._meta.get_field(generic.fk_field)
    column: Any = pk
    while column.is_relation:
        # A multi-table child's key is its parent's: compare that column.
        column = column.target_field
    kinds = {object_id.get_internal_type(), column.get_internal_type()}
    if len(kinds) > 1 and not (kinds <= _INTEGER_TYPES or kinds <= _STRING_TYPES):
        raise ValueError(
            f"{name} cannot back {allowed.type!r}: the object id field "
            f"{generic.fk_field!r} cannot hold the primary key of {target_model.__name__}"
        )
    backing = relation.backing
    assert isinstance(backing, FieldBinding)
    # Nothing joins through a GenericForeignKey: filters name the edge's own columns.
    _validate_const_filters(source_model, backing)
    return ResolvedFieldBacking(
        source_model,
        target_model,
        object_id,
        relation,
        allowed.type,
        target_id_attr,
        generic.fk_field,
        GenericColumns(generic.name, generic.ct_field, generic.fk_field),
    )


def foreign_key_kept(field: models.ForeignKey[Any, Any], using: str) -> bool:
    """Whether a value of the column proves a row of the model it names.

    The database must constrain the column, and every link of a multi-table
    model to an ancestor: its row is its own and its ancestors' together.  A
    constraint is taken to exist only on a table Django manages.
    """

    if not connections[using].features.supports_foreign_keys:
        return False
    return all(
        link.db_constraint and link.model._meta.managed  # type: ignore[attr-defined]
        for link in (field, *_parent_links(field.related_model))
    )


def _parent_links(model: type[models.Model]) -> Iterator[models.Field[Any, Any]]:
    """The links from a model's table to the tables of all its ancestors.

    A proxy has no link to the model it stands for; that model's own links
    are followed all the same.
    """
    for parent, link in model._meta.parents.items():
        if link is not None:
            yield link
        yield from _parent_links(parent)


def _proposed_forward_relationships(
    instance: models.Model,
    definition: Definition,
    *,
    required_relations: frozenset[str],
    using: str | None = None,
) -> dict[str, tuple[SubjectRef, ...] | None]:
    """Project required field-backed and filtered-constant candidate facts.

    required_relations includes named-permission and arrow-source dependencies.
    Other backings are not resolved, queried, filtered, or marked unknown.
    A reverse or many-valued first hop is genuinely empty on a new row. Forward
    paths (including inherited MTI fields) and filters use known candidate
    values and persisted targets on the write alias. Unfiltered single-hop FKs
    storing the REBAC identity project their prepared scalar without a query.
    Database values not known before insertion, or later reverse/many-valued
    hops that insertion can change, contribute None: an unknown relation,
    never an empty one.
    check_new denies unknown arms even under intersection or exclusion; an
    independent allowed union arm can still grant. Configuration/data faults
    on referenced backings continue to raise before write; missing targets
    raise where a fetch is performed.
    """

    relationships: dict[str, tuple[SubjectRef, ...] | None] = {}
    for relation in definition.relations:
        if relation.name not in required_relations:
            continue
        if isinstance(relation.backing, ConstBinding) and relation.backing.filters:
            const = resolve_const_backing(definition, relation)
            if const is None:
                raise ValueError(
                    f"Cannot preflight {definition.resource_type}#{relation.name}: "
                    "its const backing is not resolvable."
                )
            matches = const.matches_candidate(instance, using=using)
            relationships[relation.name] = (
                None
                if matches is None
                else (SubjectRef.of(const.target_resource_type, const.target_id),)
                if matches
                else ()
            )
            continue
        if not isinstance(relation.backing, FieldBinding):
            continue
        resolved = resolve_field_backing(definition, relation)
        if resolved is None:
            raise ValueError(
                f"Cannot preflight {definition.resource_type}#{relation.name}: "
                "its field backing is not resolvable."
            )
        relationships[relation.name] = _proposed_field_subjects(instance, resolved, using=using)
    return relationships


def _proposed_field_subjects(
    instance: models.Model,
    resolved: ResolvedFieldBacking,
    *,
    using: str | None,
) -> tuple[SubjectRef, ...] | None:
    """Follow scalar forward FKs without consulting related-object caches."""

    if resolved.generic is not None:
        return _proposed_generic_subjects(instance, resolved, using=using)
    relation = resolved.relation
    label = f"{model_resource_type(instance)}#{relation.name}"
    current = instance
    parts = resolved.path.split("__")
    for index, part in enumerate(parts):
        field = current._meta.get_field(part)
        if not isinstance(field, (models.ForeignKey, models.OneToOneField)):
            return () if index == 0 else None
        # concrete_fields includes fields declared on every MTI parent table.
        if field not in current._meta.concrete_fields:
            return None
        raw_target = getattr(current, field.attname)
        if _value_is_unresolved(field, raw_target):
            return None
        if raw_target is None:
            return ()
        prepared_target = field.target_field.get_prep_value(raw_target)
        if isinstance(prepared_target, (BaseExpression, Combinable)):
            raise ValueError(
                f"Cannot preflight {label}: "
                "the proposed foreign-key identity did not resolve to a scalar value."
            )
        if len(parts) == 1 and not resolved.filters and resolved.targets_identity_directly():
            target_id = field.target_field.to_python(prepared_target)
            break
        target_model = resolved.target_model if index == len(parts) - 1 else field.related_model
        targets = target_model._base_manager.db_manager(using).filter(
            **{field.target_field.name: prepared_target}
        )
        target = targets.first()
        if target is None:
            raise ValueError(
                f"Cannot preflight {label}: the proposed related object is unavailable."
            )
        if index == 0 and resolved.filters:
            filtered = _filter_candidate_targets(
                instance, field, targets, resolved.filters, using=using
            )
            if filtered is None:
                return None
            if not filtered.exists():
                return ()
        current = target
    else:
        target_id = getattr(current, resolved.target_id_attr)
    if target_id is None:
        raise ValueError(
            f"Cannot preflight {label}: the proposed related object has no REBAC identity."
        )
    allowed = relation.allowed_subjects[0]
    return (SubjectRef.of(allowed.type, str(target_id), allowed.relation),)


def _proposed_generic_subjects(
    instance: models.Model,
    resolved: ResolvedFieldBacking,
    *,
    using: str | None,
) -> tuple[SubjectRef, ...] | None:
    """The row a candidate edge names, when it is a row of the relation's type.

    An edge to another content type, to a row that is gone, or that misses
    the relation's filters, names nothing through this relation.
    """
    generic = resolved.generic
    assert generic is not None
    model = type(instance)
    content_type_field = model._meta.get_field(generic.ct_field)
    assert isinstance(content_type_field, models.ForeignKey)
    object_id_field = resolved.field
    assert isinstance(object_id_field, models.Field)
    content_type_id = getattr(instance, content_type_field.attname)
    object_id = getattr(instance, object_id_field.attname)
    if _value_is_unresolved(content_type_field, content_type_id) or _value_is_unresolved(
        object_id_field, object_id
    ):
        return None
    if content_type_id is None or object_id is None:
        return ()
    alias = using or router.db_for_write(model)
    content_type = _content_type(resolved.target_model, alias)
    if content_type is None or content_type_field.to_python(content_type_id) != content_type.pk:
        return ()
    matches = _candidate_matches(model, resolved.filters, instance, using=alias)
    if matches is not True:
        return None if matches is None else ()
    pk = resolved.target_model._meta.pk
    assert pk is not None
    key = pk.to_python(object_id)
    if not resolved.target_model._base_manager.db_manager(alias).filter(pk=key).exists():
        return ()
    return (SubjectRef.of(resolved.target_resource_type, str(key)),)


@dataclass(frozen=True, slots=True)
class ResolvedAttributeBacking:
    """A virtual container derived from a scalar field on its subject model."""

    target_model: type[models.Model]
    target_resource_type: str
    target_id_attr: str
    field: models.Field[Any, Any]
    resource: str | None
    value: Any
    filters: dict[str, Any]

    def applies_to(self, resource_id: str) -> bool:
        return self.resource is None or self.resource == resource_id

    @staticmethod
    def container_id_of(value: Any) -> str | None:
        """Wire id of the dynamic container holding a subject with ``value``.

        The container id is the canonical Python spelling of the field value.
        ``None`` and the empty string name no container.
        """
        if value is None:
            return None
        return str(value) or None

    def canonical_container_value(self, resource_id: str) -> Any | None:
        """The field value whose container id is exactly ``resource_id``.

        Round-trips ``resource_id`` through the field's Python conversion so
        only the canonical spelling matches: ``"01"`` is not integer container
        ``1`` and ``"1"`` is not boolean container ``True``. ``None`` when the
        id names no container.
        """
        try:
            value = self.field.to_python(resource_id)
        except ValidationError, ValueError, TypeError:
            return None
        if self.container_id_of(value) != resource_id:
            return None
        return value

    def subjects_filter(self, resource_id: str | Combinable) -> Q:
        """Select current subject rows belonging to the virtual container.

        ``resource_id`` is a concrete wire id, or a query expression referring
        to the resource id column when compiled inside a lazy queryset scope.
        """
        if isinstance(resource_id, str):
            if not self.applies_to(resource_id):
                return Q(pk__in=[])
            if self.resource is None:
                value = self.canonical_container_value(resource_id)
                if value is None:
                    return Q(pk__in=[])
            else:
                value = self.value
        else:
            value = resource_id if self.resource is None else self.value
        return Q(**self.filters) & Q(**{self.field.name: value})

    def target_filter(self, resource_id: str | Combinable, subject: SubjectRef) -> Q:
        if subject.subject_type != self.target_resource_type or subject.optional_relation:
            return Q(pk__in=[])
        return self.subjects_filter(resource_id) & Q(**{self.target_id_attr: subject.subject_id})

    def has_subject(self, resource_id: str, subject: SubjectRef, using: str | None = None) -> bool:
        """Whether ``subject`` is currently a member of the virtual container."""
        rows = self.target_model._base_manager.db_manager(using)
        return rows.filter(self.target_filter(resource_id, subject)).exists()

    def subject_ids(self, resource_id: str, using: str | None = None) -> QuerySet[Any, Any]:
        """Distinct identities of the subjects currently in the virtual container."""
        rows = self.target_model._base_manager.db_manager(using)
        # Clear ``Meta.ordering`` before DISTINCT: its columns would otherwise
        # join the projection and yield one row per subject row, not per id.
        return (
            rows.filter(self.subjects_filter(resource_id))
            .order_by()
            .values_list(self.target_id_attr, flat=True)
            .distinct()
        )

    def resource_ids_for_subject(self, subject: SubjectRef, using: str | None = None) -> set[str]:
        if subject.subject_type != self.target_resource_type or subject.optional_relation:
            return set()
        return self.resource_ids_for_targets((subject.subject_id,), using=using)

    def resource_ids_for_targets(
        self, target_ids: Iterable[str], using: str | None = None
    ) -> set[str]:
        """Project live subject rows back to their virtual resource IDs."""

        predicate = Q(**self.filters) & Q(**{f"{self.target_id_attr}__in": target_ids})
        rows = self.target_model._base_manager.db_manager(using).filter(predicate)
        if self.resource is not None:
            return (
                {self.resource} if rows.filter(**{self.field.name: self.value}).exists() else set()
            )
        values = rows.order_by().values_list(self.field.name, flat=True).distinct()
        ids = (self.container_id_of(value) for value in values)
        return {container_id for container_id in ids if container_id is not None}


@dataclass(frozen=True, slots=True)
class ResolvedConstBacking(_SourceFilters):
    """A fixed target with optional predicates on local source columns."""

    source_model: type[models.Model]
    relation: Relation
    target_resource_type: str
    target_id: str

    @property
    def source_id_attr(self) -> str:
        return resource_id_attr(self.source_model)

    def source_values_path(self) -> str:
        return self.source_id_attr

    def matches(self, resource_id: str, using: str | None = None) -> bool:
        """Bare constants stay row-independent; filtered edges require a match."""
        if not self.filters:
            return True
        rows = self.source_model._base_manager.db_manager(using).all()
        return rows.filter(
            Q(**self.filters)
            & model_identity_filter(
                self.source_model, self.source_id_attr, resource_id, using=rows.db
            )
        ).exists()

    def matches_candidate(self, instance: models.Model, *, using: str | None = None) -> bool | None:
        """Evaluate local column values without reading a persisted source row."""
        return _candidate_matches(self.source_model, self.filters, instance, using=using)


def _candidate_matches(
    model: type[models.Model],
    filters: dict[str, Any],
    instance: models.Model,
    *,
    using: str | None,
) -> bool | None:
    """Whether a candidate's own column values meet ``filters``; ``None`` when unknown."""
    if not filters:
        return True
    query = Query(None)
    for lookup in sorted(filters):
        field = _const_filter_field(model, lookup)
        value = getattr(instance, field.attname)
        if _value_is_unresolved(field, value):
            return None
        root = lookup.split("__", 1)[0]
        _query_field, scalar = model_identity_fields(model, root)
        query.add_annotation(_candidate_literal(scalar, value), root, select=False)
    query.add_annotation(
        Coalesce(Q(**filters), False, output_field=models.BooleanField()),
        "_rebac_const_matches",
    )
    result = query.get_compiler(using=using or DEFAULT_DB_ALIAS).execute_sql(SINGLE)
    return bool(result and result[0])


def _const_filter_field(model: type[models.Model], lookup: str) -> models.Field[Any, Any]:
    """Resolve a constant predicate to a column on its own resource model."""
    root = lookup.split("__", 1)[0]
    field = model._meta.pk if root == "pk" else model._meta.get_field(root)
    if (
        not isinstance(field, models.Field)
        or (root != "pk" and field not in model._meta.local_concrete_fields)
        or (field.is_relation and root not in {"pk", field.attname})
    ):
        raise ValueError(
            "constant filters must name local concrete fields on the resource model; "
            "inherited MTI columns and related-row traversals require a join"
        )
    return field


def _validate_const_filters(
    model: type[models.Model], backing: ConstBinding | FieldBinding
) -> None:
    if not backing.filters:
        return
    query = Query(None)
    for lookup, _value in backing.filters:
        try:
            _const_filter_field(model, lookup)
        except FieldDoesNotExist as exc:
            raise ValueError(str(exc)) from exc
        _query_field, scalar = model_identity_fields(model, lookup.split("__", 1)[0])
        query.add_annotation(Value(None, output_field=scalar), lookup.split("__", 1)[0])
    try:
        query.add_q(Q(**dict(backing.filters)))
    except (FieldError, ValueError, TypeError, ValidationError) as exc:
        raise ValueError(f"invalid constant filters on {model.__name__}: {exc}") from exc
    # Check scalar lookups AND model paths: Django accepts parent_id__name as a traversal.
    _validate_filters(model, backing.filters)


def _relation_path(
    source_model: type[models.Model],
    path: str,
    *,
    lookup: bool = False,
    visit: Callable[[type[models.Model], ModelField, str], None] | None = None,
) -> tuple[type[models.Model], ModelField, str]:
    """Resolve model hops using native metadata, optionally visiting each field.

    By default the whole path must contain relations. With lookup=True, visit
    the terminal scalar too, then stop before transforms/lookup suffixes. The
    callback receives the owning model, field, and normalized path prefix.
    """

    current = source_model
    names: list[str] = []
    last: ModelField | None = None
    for part in path.split("__"):
        try:
            last = current._meta.pk if part == "pk" else current._meta.get_field(part)
        except FieldDoesNotExist as exc:
            if lookup and last is not None:
                break
            raise ValueError(f"missing field {part!r} on {current.__name__}") from exc
        if last is None:
            raise ValueError(f"missing field {part!r} on {current.__name__}")
        names.append(last.name)
        if visit is not None:
            visit(current, last, "__".join(names))
        target = getattr(last, "related_model", None)
        if not last.is_relation or not isinstance(target, type):
            if lookup:
                break
            raise ValueError(f"{current.__name__}.{part} must be a model relation")
        current = target
    if last is None:
        raise ValueError("field path must not be empty")
    return current, last, "__".join(names)


def _validate_filters(model: type[models.Model], filters: tuple[tuple[str, Any], ...]) -> None:
    """Let Django resolve complete lookup paths without executing the query."""

    try:
        model._base_manager.filter(Q(**dict(filters)))
    except (FieldError, ValueError, TypeError, ValidationError) as exc:
        raise ValueError(f"invalid filters on {model.__name__}: {exc}") from exc


def _validate_model_identity(model: type[models.Model], attr: str) -> None:
    """Require a scalar identity that Django can project and look up."""

    field, _scalar_field = model_identity_fields(model, attr)
    try:
        expression = field.get_col(model._meta.db_table)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            f"{model.__name__} identity {attr!r} must provide a queryable column expression"
        ) from exc
    if (
        isinstance(expression, ColPairs)
        or not isinstance(expression, BaseExpression)
        or (isinstance(expression, Col) and expression.target.column is None)
    ):
        raise ValueError(
            f"{model.__name__} identity {attr!r} must provide a queryable column expression"
        )
    if field.get_lookup("exact") is None or field.get_lookup("in") is None:
        raise ValueError(f"{model.__name__} identity {attr!r} must support exact and in lookups")


def _resolve_field_backing(definition: Definition, relation: Relation) -> ResolvedFieldBacking:
    backing = relation.backing
    if not isinstance(backing, FieldBinding) or len(relation.allowed_subjects) != 1:
        raise ValueError("field-backed relation must declare exactly one subject type")
    allowed = relation.allowed_subjects[0]
    source_model = model_for_resource_type(definition.resource_type)
    target = model_for_subject_type(allowed.type)
    if source_model is None or target is None:
        missing = [
            side
            for side, found in (
                (f"resource type {definition.resource_type}", source_model),
                (f"subject type {allowed.type}", target),
            )
            if found is None
        ]
        raise ValueError(
            "field-backed relation requires a concrete Django model for " + " and ".join(missing)
        )
    target_model, target_id_attr = target
    try:
        _validate_model_identity(source_model, resource_id_attr(source_model))
        _validate_model_identity(target_model, target_id_attr)
    except FieldDoesNotExist as exc:
        raise ValueError(f"missing identity field {exc.args[0]!r}") from exc
    generic = _generic_field(source_model, backing.path)
    if generic is not None:
        return _resolve_generic_backing(
            definition, relation, source_model, generic, target_model, target_id_attr
        )
    actual_model, field, path = _relation_path(source_model, backing.path)
    if actual_model._meta.concrete_model is not target_model._meta.concrete_model:
        raise ValueError(
            f"field path {backing.path!r} points at {model_resource_type(actual_model)!r}, "
            f"but schema allows {allowed.type!r}"
        )
    _validate_filters(source_model, backing.filters)
    return ResolvedFieldBacking(
        source_model, target_model, field, relation, allowed.type, target_id_attr, path
    )


def resolve_field_backing(
    definition: Definition, relation: Relation
) -> ResolvedFieldBacking | None:
    """Resolve a field path, returning None for declarations checks will reject."""

    if not isinstance(relation.backing, FieldBinding):
        return None
    try:
        return _resolve_field_backing(definition, relation)
    except ValueError:
        return None


def _resolve_attribute_backing(
    definition: Definition, relation: Relation
) -> ResolvedAttributeBacking:
    backing = relation.backing
    if not isinstance(backing, AttributeBinding) or len(relation.allowed_subjects) != 1:
        raise ValueError("attribute-backed relation must declare exactly one subject type")
    allowed = relation.allowed_subjects[0]
    target = model_for_subject_type(allowed.type)
    if target is None:
        raise ValueError(
            f"attribute-backed relation requires a concrete Django model for subject type "
            f"{allowed.type}"
        )
    target_model, target_id_attr = target
    try:
        _validate_model_identity(target_model, target_id_attr)
    except FieldDoesNotExist as exc:
        raise ValueError(f"missing identity field {exc.args[0]!r}") from exc
    try:
        field = target_model._meta.get_field(backing.field)
    except FieldDoesNotExist as exc:
        raise ValueError(f"missing attribute {backing.field!r} on {target_model.__name__}") from exc
    if not isinstance(field, models.Field) or not field.concrete or field.is_relation:
        raise ValueError("attribute backing requires a concrete scalar subject field")
    _validate_filters(target_model, backing.filters)
    if backing.resource is not None:
        _validate_filters(target_model, ((field.name, backing.value),))
    return ResolvedAttributeBacking(
        target_model,
        allowed.type,
        target_id_attr,
        field,
        backing.resource,
        backing.value,
        dict(backing.filters),
    )


def resolve_attribute_backing(
    definition: Definition, relation: Relation
) -> ResolvedAttributeBacking | None:
    """Resolve live subject-column membership independently of a container table."""

    if not isinstance(relation.backing, AttributeBinding):
        return None
    try:
        return _resolve_attribute_backing(definition, relation)
    except ValueError:
        return None


def attribute_backing_model_errors(definition: Definition, relation: Relation) -> list[str]:
    """Return model errors for a live subject-column declaration."""

    if not isinstance(relation.backing, AttributeBinding):
        return []
    try:
        _resolve_attribute_backing(definition, relation)
    except ValueError as exc:
        return [f"{definition.resource_type}#{relation.name}: attribute backing: {exc}"]
    return []


def resolve_const_backing(
    definition: Definition,
    relation: Relation,
) -> ResolvedConstBacking | None:
    """Resolve a ``// rebac:const=...`` binding to concrete Django metadata.

    Returns ``None`` for another backing kind, missing source model or invalid
    local-column filters. The target type need not be a model (it is
    commonly a virtual role namespace such as ``platform/role``); only the source
    type must be one, because the reverse direction enumerates its rows.
    """
    backing = relation.backing
    if not isinstance(backing, ConstBinding):
        return None
    if len(relation.allowed_subjects) != 1:
        return None
    allowed = relation.allowed_subjects[0]
    source_model = model_for_resource_type(definition.resource_type)
    if source_model is None:
        return None
    try:
        _validate_const_filters(source_model, backing)
    except ValueError:
        return None
    return ResolvedConstBacking(
        source_model,
        relation,
        allowed.type,
        backing.target_id,
    )


def const_backing_model_errors(definition: Definition, relation: Relation) -> list[str]:
    """Return Django-model validation errors for a const-backed relation."""
    if not isinstance(relation.backing, ConstBinding):
        return []
    model = model_for_resource_type(definition.resource_type)
    if model is None:
        return [
            f"{definition.resource_type}#{relation.name}: const-backed relation "
            "requires a Django model with matching Meta.rebac_resource_type"
        ]
    try:
        _validate_const_filters(model, relation.backing)
    except ValueError as exc:
        return [f"{definition.resource_type}#{relation.name}: const backing: {exc}"]
    return []


def const_target_definition_errors(schema: Schema) -> list[str]:
    """Return errors for const-backed relations whose target type is undefined.

    A const relation points every row at ``<target_type>:<id>``; the target type
    need not be a Django model (it is commonly a virtual role namespace), but it
    *must* resolve to a schema ``definition`` — otherwise the arrow walk hits
    ``get_definition(...) is None`` and silently denies, turning a target-type
    typo (``org/rol`` for ``org/role``) into an invisible deny. Run against the
    effective (merged) schema so cross-package targets are present.
    """
    defined = {d.resource_type for d in schema.definitions}
    errors: list[str] = []
    for definition in schema.definitions:
        for relation in definition.relations:
            if not isinstance(relation.backing, ConstBinding):
                continue
            if len(relation.allowed_subjects) != 1:
                continue  # multi-subject is already rejected by validate_schema
            target_type = relation.allowed_subjects[0].type
            if target_type not in defined:
                errors.append(
                    f"{definition.resource_type}#{relation.name}: const-backed relation "
                    f"targets {target_type!r}, which has no schema definition"
                )
    return sorted(errors)


def const_arrow_cycle_errors(schema: Schema) -> list[str]:
    """Return an error if const arrows form an evaluation cycle.

    A const arrow ``via->target`` (``via`` const-backed, pointing at type ``T``)
    evaluates permission ``target`` on the *fixed* object ``T:<const>``. A stored
    arrow terminates because a cyclic graph needs real rows; a const arrow always
    has its synthetic edge, so two types whose const arrows point back at each
    other recurse over ``(type, permission)`` until ``PermissionDepthExceeded``
    on *every* check, with no clean deny. Detect the cycle statically at load.
    """
    # Directed graph over (resource_type, permission) nodes, following only the
    # const-arrow edges. A non-permission arrow target (a relation) or a target
    # type with no matching permission is simply a node with no outgoing edges.
    edges: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for definition in schema.definitions:
        const_targets = {
            r.name: r.allowed_subjects[0].type
            for r in definition.relations
            if isinstance(r.backing, ConstBinding) and len(r.allowed_subjects) == 1
        }
        if not const_targets:
            continue
        for perm in definition.permissions:
            node = (definition.resource_type, perm.name)
            for arrow in _const_arrows_in(perm.expression, const_targets):
                edges.setdefault(node, set()).add((const_targets[arrow.via], arrow.target))

    cycle = _find_const_arrow_cycle(edges)
    if cycle is None:
        return []
    path = " -> ".join(f"{rtype}#{perm}" for rtype, perm in cycle)
    return [
        f"const-backed relation arrow cycle: {path} — evaluation would recurse "
        "until the depth limit on every permission check"
    ]


def _const_arrows_in(expr: PermExpr, const_relations: dict[str, str]) -> Iterator[PermArrow]:
    """Yield the const-backed arrows (``via`` in ``const_relations``) in ``expr``."""
    if isinstance(expr, PermArrow):
        if expr.via in const_relations:
            yield expr
    elif isinstance(expr, PermBinOp):
        yield from _const_arrows_in(expr.left, const_relations)
        yield from _const_arrows_in(expr.right, const_relations)


def _find_const_arrow_cycle(
    edges: dict[tuple[str, str], set[tuple[str, str]]],
) -> list[tuple[str, str]] | None:
    """Return a deterministic directed cycle, using the stdlib graph walker."""
    try:
        TopologicalSorter({key: sorted(edges[key]) for key in sorted(edges)}).prepare()
    except CycleError as exc:
        # TopologicalSorter follows predecessor edges: reverse the report to
        # retain this module's source -> target diagnostic convention.
        return list(reversed(exc.args[1]))
    return None


def field_backing_model_errors(definition: Definition, relation: Relation) -> list[str]:
    """Return model errors for a live relation path and its source filters."""

    if not isinstance(relation.backing, FieldBinding):
        return []
    try:
        _resolve_field_backing(definition, relation)
    except ValueError as exc:
        return [f"{definition.resource_type}#{relation.name}: field backing: {exc}"]
    return []
