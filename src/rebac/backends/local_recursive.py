"""Bounded recursive scopes, reusing the ordinary compiler's relation joins.

SQL determines both grants and the frontier beyond the walker's dispatch bound.
Only frontier candidates need a graph check: SQL has no portable way to raise
PermissionDepthExceeded, and a multi-target arrow's early return is ordered by
the walker, not by SQL's existential membership test.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from django.db import models
from django.db.models import Q, QuerySet, Value
from django.db.models.sql.where import WhereNode

from .._id import resource_id_attr
from ..conf import app_settings
from ..schema.ast import (
    BUILTIN_ACTOR_TYPES,
    AllowedSubject,
    Definition,
    PermBinOp,
    PermExpr,
    PermNil,
    PermRef,
    Relation,
    Schema,
)
from ..schema.introspection import dispatch_edges
from ..schema.walker import find_relation
from ..types import SubjectRef
from .local_query import _ROOT_CONTEXT, LocalQueryScope, UnsupportedScope, _CompileContext, _truth

if TYPE_CHECKING:
    from .local import LocalBackend


def reaches_self_arrow(
    schema: Schema,
    resource_type: str,
    action: str,
    seen: frozenset[tuple[str, str]] = frozenset(),
) -> bool:
    """Find self arrows even when an earlier branch prevents the first compile."""
    key = (resource_type, action)
    if key in seen:
        return False
    return any(
        (edge.is_arrow and edge.resource_type == resource_type)
        or reaches_self_arrow(schema, edge.resource_type, edge.action, seen | {key})
        for edge in dispatch_edges(schema, resource_type, action)
    )


class RecursiveQueryScope(LocalQueryScope):
    def __init__(self, backend: LocalBackend, subject: SubjectRef, using: str) -> None:
        super().__init__(backend, subject, using)
        self.depth_limit = app_settings.REBAC_DEPTH_LIMIT

    def predicate(self, model: type[models.Model], action: str, resource_type: str) -> Q:
        identity = resource_id_attr(model)
        grant = self.permission(resource_type, action, model, identity, frozenset())
        frontier = self.permission(
            resource_type, action, model, identity, frozenset(), _CompileContext(boundary=True)
        )
        return Q(DepthCheckedPredicate(grant, frontier, self, model, action, resource_type))

    def permission(
        self,
        resource_type: str,
        action: str,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
    ) -> Q:
        definition = self.schema.get_definition(resource_type)
        if definition is None:
            return _truth(False)
        permission = self.schema.get_permission(resource_type, action)
        if permission is None and find_relation(definition, action) is None:
            return _truth(False)
        if (
            sum(
                edge.is_arrow and edge.resource_type == resource_type and edge.action == action
                for edge in dispatch_edges(self.schema, resource_type, action)
            )
            > 1
        ):
            raise UnsupportedScope(f"Multiple self-arrows in {resource_type}#{action}")
        if context.depth > self.depth_limit:
            return _truth(context.boundary)
        expr = permission.expression if permission is not None else PermRef(action)
        return self.expression(expr, definition, model, identity, seen, context)

    def hop(
        self,
        resource_type: str,
        action: str,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
    ) -> Q:
        return self.permission(
            resource_type,
            action,
            model,
            identity,
            frozenset(),
            replace(context, depth=context.depth + 1),
        )

    def expression(
        self,
        expr: PermExpr,
        definition: Definition,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
    ) -> Q:
        if (
            isinstance(expr, PermRef)
            and expr.name not in BUILTIN_ACTOR_TYPES
            and find_relation(definition, expr.name) is None
        ):
            # Match eval_expr: mark aliases when referenced, not when entering
            # a dispatch frame. The distinction matters for alias cycles under
            # exclusion. An arrow opens a fresh alias set in hop().
            key = (definition.resource_type, expr.name)
            if key in seen:
                return _truth(False)
            return self.permission(
                definition.resource_type, expr.name, model, identity, seen | {key}, context
            )
        if context.boundary:
            if isinstance(expr, PermBinOp):
                # Over-approximate possible dispatches, including both sides of
                # exclusions. The walker alone decides whether an early return
                # skips a frontier, so SQL never invents a new error policy.
                return self.branch(
                    expr.left, definition, model, identity, seen, context
                ) | self.branch(expr.right, definition, model, identity, seen, context)
            if isinstance(expr, PermNil):
                return _truth(False)
            if isinstance(expr, PermRef) and expr.name in BUILTIN_ACTOR_TYPES:
                return _truth(False)
        return super().expression(expr, definition, model, identity, seen, context)

    def branch(
        self,
        expr: PermExpr,
        definition: Definition,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
    ) -> Q:
        return self.expression(expr, definition, model, identity, seen, context)

    def relation(
        self,
        definition: Definition,
        relation: Relation,
        model: type[models.Model] | None,
        identity: str | Value,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
        *,
        target: str | None = None,
    ) -> Q:
        # ConvertedRelationIds enumerates the entire permission graph, which
        # cannot represent builtins. Keep that existing fallback out of the
        # bounded compiler; native field paths remain supported as before.
        if (
            relation.backing is None
            and model is not None
            and isinstance(identity, str)
            and not self.native_identity(model, identity)
        ):
            raise UnsupportedScope
        if context.boundary and target is None and relation.backing is not None:
            return _truth(False)
        return super().relation(definition, relation, model, identity, seen, context, target=target)

    def subject_membership(
        self,
        allowed: AllowedSubject,
        target: str | None,
        seen: frozenset[tuple[str, str]],
        context: _CompileContext = _ROOT_CONTEXT,
    ) -> Q:
        if context.boundary and target is None:
            if not allowed.relation:
                return _truth(False)
            return self.hop(
                allowed.type,
                allowed.relation,
                None,
                "_scope_subject_id",
                seen,
                context,
            )
        return super().subject_membership(allowed, target, seen, context)


class DepthCheckedPredicate(models.Expression):
    """Validate the live frontier at SQL compilation, without enumerating grants."""

    def __init__(
        self,
        grant: Q,
        frontier: Q,
        scope: RecursiveQueryScope,
        model: type[models.Model],
        action: str,
        resource_type: str,
    ) -> None:
        super().__init__(output_field=models.BooleanField())
        self.expressions: list[Any] = [grant, frontier]
        self.frontier = frontier
        self.scope = scope
        self.model = model
        self.action = action
        self.resource_type = resource_type

    def get_source_expressions(self) -> list[Any]:
        return self.expressions

    def set_source_expressions(self, exprs: Sequence[Any]) -> None:
        self.expressions = list(exprs)

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        query = compiler.query.clone()
        if query.external_aliases:
            # A correlated scope cannot execute outside its outer SQL query.
            # Validate conservatively over its type, with unrelabeled aliases.
            candidates = self.model._base_manager.using(self.scope.using).filter(self.frontier)
        else:
            query.where = self._frontier_where(query.where)
            query.clear_limits()
            candidates = QuerySet(model=self.model, query=query, using=self.scope.using)
        definition = self.scope.schema.get_definition(self.resource_type)
        assert definition is not None
        permission = self.scope.schema.get_permission(self.resource_type, self.action)
        expr = permission.expression if permission is not None else PermRef(self.action)
        # Stream potential overflows only. For a bounded graph this is a single
        # empty SQL result, regardless of how many resources receive a grant.
        for resource_id in (
            candidates.order_by().values_list(resource_id_attr(self.model), flat=True).iterator()
        ):
            self.scope.backend._eval_permission(
                expr,
                definition,
                str(resource_id),
                self.scope.subject,
                0,
                using=self.scope.using,
            )
        sql, params = compiler.compile(self.expressions[0])
        return str(sql), tuple(params)

    def _frontier_where(self, node: WhereNode) -> WhereNode:
        # Django wraps conditional expressions in Exact(expression, True).
        # replace_expressions traverses that wrapper as well as WhereNodes.
        return node.replace_expressions({self: self.expressions[1]})
