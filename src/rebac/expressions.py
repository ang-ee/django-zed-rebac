"""Lazy, actor-explicit Django permission projections."""

from __future__ import annotations

from typing import Any, cast

from django.db import models
from django.db.models.expressions import BaseExpression
from django.utils.functional import cached_property

from ._id import model_identity_fields, resource_id_attr
from .actors import ActorLike, to_subject_ref
from .database import database_context
from .resources import model_resource_type
from .types import SubjectRef


def permission_expression(
    model: type[models.Model], action: str, *, actor: ActorLike
) -> models.Expression:
    """Project definite permission for this row.

    The explicit actor is captured now; ambient actor/sudo never grants this
    capability. The executing database and schema bind later. The owner must be
    the query's model, its physical proxy equivalent, or an MTI ancestor.
    Denied, conditional and SQL NULL answers are false. No rows are fetched at
    construction. Normally the policy binds during SQL compilation. A
    compatible union owner prepares this projection earlier under its pinned
    policy. This differs from the early-pinned ``queryset_filter`` API.
    """
    if not action:
        raise ValueError("permission_expression() requires an action")
    if model_resource_type(model) is None:
        raise ValueError("permission_expression() requires a REBAC resource model")
    model_identity_fields(model, resource_id_attr(model))
    return _Permission(model, action, to_subject_ref(actor))


class _Permission(models.Expression):
    @cached_property
    def contains_subquery(self) -> bool:
        return True

    def __init__(self, model: type[models.Model], action: str, actor: SubjectRef) -> None:
        super().__init__(output_field=models.BooleanField())
        self.model = model
        self.action = action
        self.actor = actor
        self.identity: Any = models.F(resource_id_attr(model))
        self.owner_lookup = "pk"
        self.row_model: type[models.Model] | None = None
        # Resolution makes shallow copies; all copies identify one projection
        # when a union owner shares its permission predicate.
        self._rebac_permission_token = object()

    def get_source_expressions(self) -> list[Any]:
        return [self.identity]

    def set_source_expressions(self, exprs: Any) -> None:
        [self.identity] = exprs

    def resolve_expression(
        self,
        query: Any = None,
        allow_joins: bool = True,
        reuse: Any = None,
        summarize: bool = False,
        for_save: bool = False,
    ) -> Any:
        if self.row_model is not None:
            return super().resolve_expression(query, allow_joins, reuse, summarize, for_save)
        row_model = getattr(query, "model", None)
        if row_model is None:
            raise ValueError("A permission expression requires a model query")
        if row_model._meta.concrete_model is self.model._meta.concrete_model:
            path: list[str] = []
        elif self.model in row_model._meta.get_parent_list():
            path = [part.join_field.name for part in row_model._meta.get_path_to_parent(self.model)]
        else:
            raise ValueError(
                f"Permission owner {self.model.__name__} is not an ancestor of {row_model.__name__}"
            )
        clone = self.copy()
        clone.row_model = row_model
        clone.owner_lookup = "__".join([*path, "pk"])
        clone.identity = models.F("__".join([*path, resource_id_attr(self.model)]))
        return super(_Permission, clone).resolve_expression(
            query, allow_joins, reuse, summarize, for_save
        )

    def as_sql(self, compiler: Any, connection: Any) -> Any:
        from .backends import backend
        from .managers import RebacQuerySet

        if self.row_model is None:
            raise ValueError("Resolve a permission expression into its model query first")
        using = connection.alias
        with database_context(using):
            plan: Any = getattr(compiler.query, "_rebac_subject_plan", ((), (), {}, None))
            shared = (
                plan[2].get(self._rebac_permission_token)
                if plan[3] == using
                and self.row_model._meta.concrete_model is self.model._meta.concrete_model
                else None
            )
            condition: BaseExpression | None = shared
            if condition is None:
                active = backend()
                if self.row_model._meta.concrete_model is self.model._meta.concrete_model:
                    condition = active.permission_expression(
                        model=self.model,
                        subject=self.actor,
                        action=self.action,
                        using=using,
                        identity=self.identity,
                    )
            if condition is None:
                rows = self.model._default_manager.all().using(using)
                if not isinstance(rows, RebacQuerySet):
                    rows = RebacQuerySet(model=self.model, query=rows.query.clone(), using=using)
                rows = rows.with_actor(self.actor).with_action(self.action).scoped_for_aggregate()
                condition = models.Exists(rows.filter(pk=models.OuterRef(self.owner_lookup)))
            expression = models.Case(
                models.When(condition, then=models.Value(True)),
                default=models.Value(False),
                output_field=models.BooleanField(),
            ).resolve_expression(compiler.query)
            return compiler.compile(cast(models.Expression, expression))
