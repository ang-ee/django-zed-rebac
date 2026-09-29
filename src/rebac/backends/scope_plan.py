"""Opaque, uncorrelated SQL plans owned by the operation evaluator."""

from __future__ import annotations

from typing import Any

from django.db import models
from django.db.models.sql import Query

from .._id import resource_id_attr


class StandaloneIds(models.Expression):
    """Retain a query without exposing it to caller clone/relabel traversal.

    The query is built once, has no outer references, and is compiled with its
    own compiler. Only SQL is shared, never rows or execution-time parameters.
    """

    subquery = True

    def __init__(self, query: Query) -> None:
        super().__init__(output_field=query.output_field)
        self._id_query = query

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, tuple[Any, ...]]:
        sql, params = self._id_query.get_compiler(connection=connection).as_sql()
        return f"({sql})", tuple(params)

    def get_group_by_cols(self) -> list[Any]:
        return []


def compile_scope_plan(model: type[models.Model], predicate: models.Q, using: str) -> models.Q:
    """Build the grant query once; leave frontier validation on the caller."""
    from .local_flat import FlatPredicate
    from .local_recursive import DepthCheckedPredicate

    identity = resource_id_attr(model)

    def ids(condition: Any) -> models.Q:
        query = model._base_manager.using(using).filter(condition).order_by().values(identity).query
        return models.Q(**{f"{identity}__in": StandaloneIds(query)})

    if len(predicate.children) == 1 and isinstance(predicate.children[0], DepthCheckedPredicate):
        checked = predicate.children[0]
        # Its two inputs become tiny IN expressions. In particular, validation
        # still uses the caller's filters instead of widening to the whole type
        # merely because the grant query is standalone.
        return models.Q(
            DepthCheckedPredicate(
                ids(FlatPredicate(checked.expressions[0])),
                ids(FlatPredicate(checked.expressions[1])),
                checked.scope,
                model,
                checked.action,
                checked.resource_type,
            )
        )
    return ids(predicate)
