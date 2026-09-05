"""Resolution helpers for schema-declared field-backed relations."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import models

from ._id import resource_id_attr, subject_id_attr
from .conf import app_settings
from .resources import model_for_resource_type, model_resource_type
from .schema.ast import (
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


@dataclass(frozen=True, slots=True)
class ResolvedFieldBacking:
    source_model: type[models.Model]
    target_model: type[models.Model]
    field: models.Field[Any, Any]
    relation: Relation
    target_resource_type: str
    target_id_attr: str

    @property
    def source_id_attr(self) -> str:
        return resource_id_attr(self.source_model)

    def source_filter(self, resource_id: str) -> dict[str, str]:
        return {self.source_id_attr: resource_id}

    def target_filter(self, subject: SubjectRef) -> dict[str, str]:
        if self.target_id_attr == "pk":
            return {self.field.attname: subject.subject_id}
        return {f"{self.field.name}__{self.target_id_attr}": subject.subject_id}

    def target_in_filter(self, resource_ids: set[str]) -> dict[str, list[str]]:
        values = list(resource_ids)
        if self.target_id_attr == "pk":
            return {f"{self.field.attname}__in": values}
        return {f"{self.field.name}__{self.target_id_attr}__in": values}

    def source_values_path(self) -> str:
        return self.source_id_attr

    def target_values_path(self) -> str:
        if self.target_id_attr == "pk":
            return self.field.attname
        return f"{self.field.name}__{self.target_id_attr}"


@dataclass(frozen=True, slots=True)
class ResolvedConstBacking:
    """A const-backed relation resolved against the declaring Django model.

    Unlike :class:`ResolvedFieldBacking` there is no source field and the
    target object id is fixed for every row, so the forward direction needs no
    query at all; only the reverse (``accessible``) direction touches the DB,
    to enumerate every row of the source type when the constant target grants
    access.
    """

    source_model: type[models.Model]
    relation: Relation
    target_resource_type: str
    target_id: str

    @property
    def source_id_attr(self) -> str:
        return resource_id_attr(self.source_model)

    def source_values_path(self) -> str:
        return self.source_id_attr


def resolve_field_backing(
    definition: Definition,
    relation: Relation,
) -> ResolvedFieldBacking | None:
    """Resolve a field binding to concrete Django model metadata.

    Returns ``None`` when the schema names a backing that cannot be resolved
    against loaded Django models. System checks report that mismatch at
    startup; hot-path authorization calls fail closed.
    """
    backing = relation.backing
    if not isinstance(backing, FieldBinding) or len(relation.allowed_subjects) != 1:
        return None

    allowed = relation.allowed_subjects[0]
    source_model = model_for_resource_type(definition.resource_type)
    target_model, target_id_attr = _target_model_and_id_attr(allowed.type)
    if source_model is None or target_model is None:
        return None
    try:
        field = source_model._meta.get_field(backing.attname)
    except FieldDoesNotExist:
        return None
    if not isinstance(field, (models.ForeignKey, models.OneToOneField)):
        return None
    if field.remote_field is None or field.remote_field.model is not target_model:
        return None
    return ResolvedFieldBacking(
        source_model,
        target_model,
        field,
        relation,
        allowed.type,
        target_id_attr,
    )


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
    """Return Django-model validation errors for a field-backed relation."""
    backing = relation.backing
    if not isinstance(backing, FieldBinding):
        return []

    errors: list[str] = []
    source_model = model_for_resource_type(definition.resource_type)
    if source_model is None:
        return [
            f"{definition.resource_type}#{relation.name}: field-backed relation "
            "requires a Django model with matching Meta.rebac_resource_type"
        ]

    try:
        field = source_model._meta.get_field(backing.attname)
    except FieldDoesNotExist:
        return [
            f"{definition.resource_type}#{relation.name}: field-backed relation "
            f"references missing field {backing.attname!r} on {source_model.__name__}"
        ]

    if not isinstance(field, (models.ForeignKey, models.OneToOneField)):
        errors.append(
            f"{definition.resource_type}#{relation.name}: field {source_model.__name__}."
            f"{field.name} must be a ForeignKey or OneToOneField"
        )
        return errors

    if len(relation.allowed_subjects) != 1:
        return errors

    allowed = relation.allowed_subjects[0]
    target_model = getattr(field.remote_field, "model", None)
    allowed_model, _target_id_attr = _target_model_and_id_attr(allowed.type)
    if target_model is not allowed_model:
        actual_type = model_resource_type(target_model)
        errors.append(
            f"{definition.resource_type}#{relation.name}: field {source_model.__name__}."
            f"{field.name} points at resource type {actual_type!r}, "
            f"but schema allows {allowed.type!r}"
        )
    return errors


def _target_model_and_id_attr(subject_type: str) -> tuple[type[models.Model] | None, str]:
    if subject_type == app_settings.REBAC_USER_TYPE:
        user_model = get_user_model()
        return user_model, subject_id_attr(user_model)
    if subject_type == app_settings.REBAC_GROUP_TYPE:
        from django.contrib.auth.models import Group

        return Group, subject_id_attr(Group)
    model = model_for_resource_type(subject_type)
    if model is None:
        return None, ""
    return model, resource_id_attr(model)
