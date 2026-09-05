"""Stable permission-schema introspection helpers.

The AST node classes remain implementation details. Downstream callers should
ask these helpers about dependency and reachability facts instead of walking
``PermRef`` / ``PermArrow`` / ``PermBinOp`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..types import ObjectRef
from .ast import (
    BUILTIN_ACTOR_TYPES,
    ConstBinding,
    Definition,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermNil,
    PermRef,
    Relation,
    Schema,
)
from .walker import find_permission, find_relation


@dataclass(frozen=True, slots=True)
class PermissionSources:
    """Source terms reached by a permission expression."""

    direct_relations: frozenset[str]
    arrows: frozenset[tuple[str, str]]
    builtins: frozenset[str]
    subpermissions: frozenset[str]


def _empty_sources() -> PermissionSources:
    return PermissionSources(frozenset(), frozenset(), frozenset(), frozenset())


def permission_sources(
    schema: Schema,
    resource_type: str,
    permission: str,
) -> PermissionSources:
    """Return relation, arrow, builtin, and sub-permission sources for a permission."""
    definition = schema.get_definition(resource_type)
    if definition is None:
        return _empty_sources()

    relation = find_relation(definition, permission)
    if relation is not None:
        return PermissionSources(frozenset({permission}), frozenset(), frozenset(), frozenset())

    permission_def = find_permission(definition, permission)
    if permission_def is None:
        return _empty_sources()

    direct_relations: set[str] = set()
    arrows: set[tuple[str, str]] = set()
    builtins: set[str] = set()
    subpermissions: set[str] = set()
    _collect_sources(
        permission_def.expression,
        definition=definition,
        direct_relations=direct_relations,
        arrows=arrows,
        builtins=builtins,
        subpermissions=subpermissions,
        seen=frozenset({permission}),
    )
    return PermissionSources(
        frozenset(direct_relations),
        frozenset(arrows),
        frozenset(builtins),
        frozenset(subpermissions),
    )


def relation_dependencies(
    schema: Schema,
    resource_type: str,
    permission: str,
) -> frozenset[str]:
    """Return relation names used directly or as arrow ``via`` relations."""
    sources = permission_sources(schema, resource_type, permission)
    return sources.direct_relations | frozenset(via for via, _target in sources.arrows)


def permissions_reaching_relation(
    schema: Schema,
    resource_type: str,
    relation: str,
) -> frozenset[str]:
    """Return permission names whose expression depends on ``relation``."""
    definition = schema.get_definition(resource_type)
    if definition is None:
        return frozenset()
    return frozenset(
        permission.name
        for permission in definition.permissions
        if relation in relation_dependencies(schema, resource_type, permission.name)
    )


def permission_object_sources(
    schema: Schema,
    resource_type: str,
    permission: str,
    *,
    object_type: str,
) -> frozenset[ObjectRef]:
    """Return statically named objects in positive permission source branches.

    This is schema introspection, never authorization: tuples, caveats and
    intersections can make a reported source ineffective. Union/intersection
    visit both operands; exclusion omits its right subtree. Arrows follow the
    target permission or relation, and direct subject sets follow their declared
    relation. Generic/wildcard subjects contribute no invented object IDs.
    """
    refs: set[ObjectRef] = set()

    def visit(name: str, term: str, seen: frozenset[tuple[str, str]]) -> None:
        key = (name, term)
        if key in seen:
            return
        definition = schema.get_definition(name)
        if definition is None:
            return
        seen = seen | {key}
        relation = find_relation(definition, term)
        if relation is not None:
            visit_relation(relation, seen)
        else:
            selected = find_permission(definition, term)
            if selected is not None:
                visit_expr(definition, selected.expression, seen)

    def visit_relation(
        relation: Relation,
        seen: frozenset[tuple[str, str]],
        *,
        arrow_target: str | None = None,
    ) -> None:
        const_id = relation.backing.target_id if isinstance(relation.backing, ConstBinding) else ""
        for allowed in relation.allowed_subjects:
            target = arrow_target if arrow_target is not None else allowed.relation
            if target:
                definition = schema.get_definition(allowed.type)
                if definition is None or (
                    find_relation(definition, target) is None
                    and find_permission(definition, target) is None
                ):
                    continue
            object_id = const_id or allowed.id
            if allowed.type == object_type and object_id and not allowed.wildcard:
                refs.add(ObjectRef(object_type, object_id))
            if target:
                visit(allowed.type, target, seen)

    def visit_expr(
        definition: Definition,
        expr: PermExpr,
        seen: frozenset[tuple[str, str]],
    ) -> None:
        if isinstance(expr, PermNil):
            return
        if isinstance(expr, PermRef):
            if expr.name not in BUILTIN_ACTOR_TYPES:
                visit(definition.resource_type, expr.name, seen)
            return
        if isinstance(expr, PermArrow):
            relation = find_relation(definition, expr.via)
            if relation is not None:
                visit_relation(relation, seen, arrow_target=expr.target)
            return
        if isinstance(expr, PermBinOp):
            visit_expr(definition, expr.left, seen)
            if expr.op != "-":
                visit_expr(definition, expr.right, seen)
            return
        raise TypeError(f"unknown PermExpr: {expr!r}")

    visit(resource_type, permission, frozenset())
    return frozenset(refs)


def _collect_sources(
    expr: PermExpr,
    *,
    definition: Definition,
    direct_relations: set[str],
    arrows: set[tuple[str, str]],
    builtins: set[str],
    subpermissions: set[str],
    seen: frozenset[str],
) -> None:
    if isinstance(expr, PermNil):
        return
    if isinstance(expr, PermRef):
        if expr.name in BUILTIN_ACTOR_TYPES:
            builtins.add(expr.name)
            return
        if find_relation(definition, expr.name) is not None:
            direct_relations.add(expr.name)
            return
        subpermission = find_permission(definition, expr.name)
        if subpermission is None or expr.name in seen:
            return
        subpermissions.add(expr.name)
        _collect_sources(
            subpermission.expression,
            definition=definition,
            direct_relations=direct_relations,
            arrows=arrows,
            builtins=builtins,
            subpermissions=subpermissions,
            seen=seen | {expr.name},
        )
        return
    if isinstance(expr, PermArrow):
        arrows.add((expr.via, expr.target))
        return
    if isinstance(expr, PermBinOp):
        _collect_sources(
            expr.left,
            definition=definition,
            direct_relations=direct_relations,
            arrows=arrows,
            builtins=builtins,
            subpermissions=subpermissions,
            seen=seen,
        )
        _collect_sources(
            expr.right,
            definition=definition,
            direct_relations=direct_relations,
            arrows=arrows,
            builtins=builtins,
            subpermissions=subpermissions,
            seen=seen,
        )
        return
    raise TypeError(f"unknown PermExpr: {expr!r}")


__all__ = [
    "PermissionSources",
    "permission_object_sources",
    "permission_sources",
    "permissions_reaching_relation",
    "relation_dependencies",
]
