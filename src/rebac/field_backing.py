"""Resolve schema-declared live ORM and constant relations."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.core.exceptions import FieldDoesNotExist, FieldError, ValidationError
from django.db import models
from django.db.models import Q, QuerySet
from django.db.models.expressions import BaseExpression, Col, ColPairs, Combinable

from ._id import resource_id_attr
from .resources import model_for_resource_type, model_for_subject_type, model_resource_type
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
from .types import SubjectRef

if TYPE_CHECKING:
    # ``models.Field`` is generic only in django-stubs; subscripting it at
    # runtime raises ``TypeError``. Annotations are lazy (PEP 563), so the alias
    # is needed by type checkers only.
    from django.db.models.fields.reverse_related import ForeignObjectRel

    ModelField = models.Field[Any, Any] | ForeignObjectRel


@dataclass(frozen=True, slots=True)
class ResolvedFieldBacking:
    """A forward/reverse ORM path with filters anchored on its source model."""

    source_model: type[models.Model]
    target_model: type[models.Model]
    field: ModelField
    relation: Relation
    target_resource_type: str
    target_id_attr: str
    path: str

    @property
    def source_id_attr(self) -> str:
        return resource_id_attr(self.source_model)

    @property
    def filters(self) -> dict[str, Any]:
        backing = self.relation.backing
        return dict(backing.filters) if isinstance(backing, FieldBinding) else {}

    def source_filter(self, resource_id: str) -> dict[str, str]:
        return {self.source_id_attr: resource_id}

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

    def target_values_path(self) -> str:
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

        predicate = Q(**self.filters)
        if resource_id is not None:
            predicate &= Q(**self.source_filter(resource_id))
        if subject is not None:
            predicate &= Q(**self.target_filter(subject))
        if target_ids is not None:
            predicate &= Q(**self.target_in_filter(target_ids))
        return self.source_model._base_manager.db_manager(using).filter(predicate)


def _proposed_forward_relationships(
    instance: models.Model,
    definition: Definition,
    *,
    using: str | None = None,
) -> dict[str, tuple[SubjectRef, ...]]:
    """Project schema-declared forward relations from one unsaved candidate.

    Create authorization can only use facts already resolved on the candidate.
    Reverse, many-valued, filtered, and database-default-backed relations need
    a persisted row or database evaluation, so they fail closed before write.
    """

    relationships: dict[str, tuple[SubjectRef, ...]] = {}
    for relation in definition.relations:
        if not isinstance(relation.backing, FieldBinding):
            continue
        resolved = resolve_field_backing(definition, relation)
        if resolved is None:
            raise ValueError(
                f"Cannot preflight {definition.resource_type}#{relation.name}: "
                "its field backing is not resolvable."
            )
        field = resolved.field
        if (
            "__" in resolved.path
            or not isinstance(field, (models.ForeignKey, models.OneToOneField))
            or field.model._meta.concrete_model is not type(instance)._meta.concrete_model
        ):
            raise ValueError(
                f"Cannot preflight {definition.resource_type}#{relation.name}: "
                "create candidates support only direct forward ForeignKey or OneToOneField backings."
            )
        if resolved.filters:
            raise ValueError(
                f"Cannot preflight {definition.resource_type}#{relation.name}: "
                "filtered field backings require persisted query evaluation."
            )
        raw_target = getattr(instance, field.attname)
        if isinstance(raw_target, BaseExpression):
            raise ValueError(
                f"Cannot preflight {definition.resource_type}#{relation.name}: "
                "expression-backed relationships are unresolved before insert."
            )
        if raw_target is None:
            relationships[relation.name] = ()
            continue
        prepared_target = field.target_field.get_prep_value(raw_target)
        if isinstance(prepared_target, BaseExpression):
            raise ValueError(
                f"Cannot preflight {definition.resource_type}#{relation.name}: "
                "the proposed foreign-key identity did not resolve to a scalar value."
            )
        if resolved.targets_identity_directly():
            target_id = field.target_field.to_python(prepared_target)
        else:
            target = (
                resolved.target_model._base_manager.db_manager(using)
                .filter(**{field.target_field.name: prepared_target})
                .first()
            )
            if target is None:
                raise ValueError(
                    f"Cannot preflight {definition.resource_type}#{relation.name}: "
                    "the proposed related object is unavailable."
                )
            target_id = getattr(target, resolved.target_id_attr)
        if target_id is None:
            raise ValueError(
                f"Cannot preflight {definition.resource_type}#{relation.name}: "
                "the proposed related object has no REBAC identity."
            )
        allowed = relation.allowed_subjects[0]
        relationships[relation.name] = (
            SubjectRef.of(allowed.type, str(target_id), allowed.relation),
        )
    return relationships


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

    def has_any_subject(
        self, resource_id: str, target_ids: Iterable[str], using: str | None = None
    ) -> bool:
        """Whether any subject identified in ``target_ids`` is in the virtual container."""
        rows = self.target_model._base_manager.db_manager(using)
        predicate = self.subjects_filter(resource_id) & Q(
            **{f"{self.target_id_attr}__in": target_ids}
        )
        return rows.filter(predicate).exists()

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
class ResolvedConstBacking:
    """A fixed target with source rows needed only for reverse enumeration."""

    source_model: type[models.Model]
    relation: Relation
    target_resource_type: str
    target_id: str

    @property
    def source_id_attr(self) -> str:
        return resource_id_attr(self.source_model)

    def source_values_path(self) -> str:
        return self.source_id_attr


def _relation_path(
    source_model: type[models.Model], path: str
) -> tuple[type[models.Model], ModelField, str]:
    """Resolve every path segment through Django's native relation metadata."""

    current = source_model
    names: list[str] = []
    last: ModelField | None = None
    for part in path.split("__"):
        try:
            last = current._meta.get_field(part)
        except FieldDoesNotExist as exc:
            raise ValueError(f"missing field {part!r} on {current.__name__}") from exc
        target = getattr(last, "related_model", None)
        if not last.is_relation or not isinstance(target, type):
            raise ValueError(f"{current.__name__}.{part} must be a model relation")
        names.append(last.name)
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


def model_identity_fields(
    model: type[models.Model], attr: str
) -> tuple[models.Field[Any, Any], models.Field[Any, Any]]:
    """Return the query field and scalar conversion owner for an identity.

    Django exposes an MTI child primary key as its parent-link ``OneToOneField``.
    Relation attnames likewise address the stored scalar, while relation names
    materialize model instances and cannot be wire identities.
    """

    field = model._meta.pk if attr == "pk" else model._meta.get_field(attr)
    if not isinstance(field, models.Field) or isinstance(field, models.CompositePrimaryKey):
        raise ValueError(f"{model.__name__} identity {attr!r} must be a scalar field")
    scalar_field = field
    if field.is_relation:
        if not isinstance(field, (models.ForeignKey, models.OneToOneField)) or (
            attr != "pk" and attr != field.attname
        ):
            raise ValueError(f"{model.__name__} identity {attr!r} must be a scalar field")
    seen: set[int] = set()
    while scalar_field.is_relation:
        if id(scalar_field) in seen or not isinstance(
            scalar_field, (models.ForeignKey, models.OneToOneField)
        ):
            raise ValueError(f"{model.__name__} identity {attr!r} must be a scalar field")
        seen.add(id(scalar_field))
        scalar_field = scalar_field.target_field
    if isinstance(scalar_field, models.CompositePrimaryKey):
        raise ValueError(f"{model.__name__} identity {attr!r} must be a scalar field")
    return field, scalar_field


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
        raise ValueError("field-backed relation requires matching source and target Django models")
    target_model, target_id_attr = target
    try:
        _validate_model_identity(source_model, resource_id_attr(source_model))
        _validate_model_identity(target_model, target_id_attr)
    except FieldDoesNotExist as exc:
        raise ValueError(f"missing identity field {exc.args[0]!r}") from exc
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
        raise ValueError("attribute-backed relation requires a matching subject Django model")
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

    Returns ``None`` when the binding is not a const backing or the declaring
    type has no loaded Django model. The target type need not be a model (it is
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
    if model_for_resource_type(definition.resource_type) is None:
        return [
            f"{definition.resource_type}#{relation.name}: const-backed relation "
            "requires a Django model with matching Meta.rebac_resource_type"
        ]
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
    """Return one cycle (as an ordered node path closing on itself) or ``None``.

    Plain DFS with white/grey/black colouring. Iteration is ``sorted`` so the
    reported cycle is deterministic across runs / Python versions.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[tuple[str, str], int] = {}
    stack: list[tuple[str, str]] = []

    def visit(node: tuple[str, str]) -> list[tuple[str, str]] | None:
        color[node] = GREY
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            state = color.get(nxt, WHITE)
            if state == GREY:
                return [*stack[stack.index(nxt) :], nxt]
            if state == WHITE and nxt in edges:
                found = visit(nxt)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for start in sorted(edges):
        if color.get(start, WHITE) == WHITE:
            found = visit(start)
            if found is not None:
                return found
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
