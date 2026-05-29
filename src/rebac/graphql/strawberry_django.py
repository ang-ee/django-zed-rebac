"""Strawberry-Django optimizer integration with REBAC-safe relation loading."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, cast

from django.db import models
from django.db.models import QuerySet
from django.db.models.manager import BaseManager
from graphql import GraphQLResolveInfo
from graphql.language.ast import OperationType
from strawberry.types import Info
from strawberry_django.optimizer import (
    DjangoOptimizerExtension as _DjangoOptimizerExtension,
)
from strawberry_django.optimizer import (
    OptimizerConfig,
    OptimizerStore,
)
from strawberry_django.optimizer import (
    optimize as _strawberry_django_optimize,
)
from strawberry_django.resolvers import django_fetch

from .._id import resource_id_attr
from ..actors import current_actor
from ..relation_loading import related_model_for_lookup, selected_related_paths
from ..resources import model_resource_type


def optimize[M: models.Model](
    qs: QuerySet[M] | BaseManager[M],
    info: Any,
    *,
    config: OptimizerConfig | None = None,
    store: OptimizerStore | None = None,
) -> QuerySet[M]:
    """Optimize a Strawberry-Django queryset without bypassing REBAC guards."""
    if isinstance(qs, BaseManager):
        qs = qs.all()

    qs = _pin_current_actor(qs)
    safe_config = _safe_config(config)
    optimized = _strawberry_django_optimize(qs=qs, info=info, config=safe_config, store=store)
    optimized = _ensure_resource_id_selected(optimized)
    return _apply_rebac_relation_loading(optimized)


class RebacDjangoOptimizerExtension(_DjangoOptimizerExtension):
    """Strawberry-Django optimizer that preserves REBAC relation safety."""

    def __init__(
        self,
        *,
        enable_only_optimization: bool = True,
        enable_select_related_optimization: bool = True,
        enable_prefetch_related_optimization: bool = True,
        enable_annotate_optimization: bool = True,
        enable_nested_relations_prefetch: bool = True,
        execution_context: Any | None = None,
        prefetch_custom_queryset: bool = True,
    ) -> None:
        # Force custom querysets for prefetching. Strawberry-Django's default
        # is _base_manager, which this package intentionally leaves unfiltered.
        super().__init__(
            enable_only_optimization=enable_only_optimization,
            enable_select_related_optimization=enable_select_related_optimization,
            enable_prefetch_related_optimization=enable_prefetch_related_optimization,
            enable_annotate_optimization=enable_annotate_optimization,
            enable_nested_relations_prefetch=enable_nested_relations_prefetch,
            execution_context=execution_context,
            prefetch_custom_queryset=True,
        )

    def resolve(  # type: ignore[override]
        self,
        next_: Callable[..., Any],
        root: Any,
        info: GraphQLResolveInfo,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        ret = next_(root, info, *args, **kwargs)
        if not self.enabled.get():
            return ret

        if isinstance(ret, BaseManager):
            ret = ret.all()

        if isinstance(ret, QuerySet) and ret._result_cache is None:
            config = OptimizerConfig(
                enable_only=(
                    self.enable_only and info.operation.operation == OperationType.QUERY
                ),
                enable_select_related=self.enable_select_related,
                enable_prefetch_related=self.enable_prefetch_related,
                enable_annotate=self.enable_annotate_optimization,
                enable_nested_relations_prefetch=self.enable_nested_relations_prefetch,
                prefetch_custom_queryset=True,
            )
            ret = django_fetch(optimize(qs=ret, info=info, config=config))

        return ret

    def optimize[M: models.Model](
        self,
        qs: QuerySet[M] | BaseManager[M],
        info: Any,
        *,
        store: OptimizerStore | None = None,
    ) -> QuerySet[M]:
        if not self.enabled.get():
            if isinstance(qs, BaseManager):
                return qs.all()
            return qs

        operation = info._raw_info.operation if isinstance(info, Info) else info.operation
        config = OptimizerConfig(
            enable_only=self.enable_only and operation.operation == OperationType.QUERY,
            enable_select_related=self.enable_select_related,
            enable_prefetch_related=self.enable_prefetch_related,
            enable_annotate=self.enable_annotate_optimization,
            enable_nested_relations_prefetch=self.enable_nested_relations_prefetch,
            prefetch_custom_queryset=True,
        )
        return optimize(qs, info, config=config, store=store)


def _safe_config(config: OptimizerConfig | None) -> OptimizerConfig:
    if config is None:
        return OptimizerConfig(prefetch_custom_queryset=True)
    return dataclasses.replace(config, prefetch_custom_queryset=True)


def _pin_current_actor[M: models.Model](qs: QuerySet[M]) -> QuerySet[M]:
    if getattr(qs, "_rebac_actor", None) is not None:
        return qs
    if getattr(qs, "_rebac_sudo_reason", None) is not None:
        return qs
    actor = current_actor()
    if actor is None:
        return qs
    with_actor = getattr(qs, "with_actor", None)
    if callable(with_actor):
        return cast(QuerySet[M], with_actor(actor))
    return qs


def _apply_rebac_relation_loading[M: models.Model](qs: QuerySet[M]) -> QuerySet[M]:
    select_related = getattr(qs.query, "select_related", None)
    paths = selected_related_paths(qs.model, select_related)
    if paths:
        rebac_select_related = getattr(qs, "rebac_select_related", None)
        if callable(rebac_select_related):
            qs = cast(QuerySet[M], rebac_select_related(*paths))

    prefetches = tuple(getattr(qs, "_prefetch_related_lookups", ()) or ())
    if prefetches:
        cleared = qs.prefetch_related(None)
        rebac_prefetch_related = getattr(cleared, "rebac_prefetch_related", None)
        if callable(rebac_prefetch_related):
            qs = cleared
            qs = cast(QuerySet[M], rebac_prefetch_related(*prefetches))
    return qs


def _ensure_resource_id_selected[M: models.Model](qs: QuerySet[M]) -> QuerySet[M]:
    if not model_resource_type(qs.model):
        return qs
    fields, defer = qs.query.deferred_loading
    if defer or not fields:
        return qs
    attr = resource_id_attr(qs.model)
    if "." in attr:
        return qs
    wanted = set(fields)
    wanted.update(_resource_id_field_names(qs.model, attr))
    for path in selected_related_paths(qs.model, qs.query.select_related):
        target = related_model_for_lookup(qs.model, path)
        if target is None or not model_resource_type(target):
            continue
        related_attr = resource_id_attr(target)
        if "." in related_attr:
            continue
        for name in _resource_id_field_names(target, related_attr):
            wanted.add(f"{path}__{name}")
    return qs.only(*wanted)


def _resource_id_field_names(model: type[models.Model], attr: str) -> set[str]:
    if attr == "pk":
        pk = model._meta.pk
        if pk is None:
            return set()
        return {pk.name, pk.attname}
    names = {attr}
    try:
        field = model._meta.get_field(attr)
    except Exception:
        return names
    names.add(field.name)
    names.add(getattr(field, "attname", field.name))
    return names


__all__ = ["RebacDjangoOptimizerExtension", "optimize"]
