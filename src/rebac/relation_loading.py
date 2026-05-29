"""Permission-aware relation loading helpers for Django querysets."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Sequence
from typing import Any, cast

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Prefetch, QuerySet

from .actors import current_actor
from .backends import backend
from .errors import MissingActorError, PermissionDenied
from .field_visibility import (
    accessible_ids,
    apply_field_visibility,
    backend_grants_all,
)
from .resources import model_resource_type
from .types import FieldDenyMode, SubjectRef


def relation_actor(queryset: Any) -> SubjectRef | None:
    """Return the actor that should guard related resources for ``queryset``."""
    actor = getattr(queryset, "_rebac_actor", None)
    if actor is not None:
        return cast(SubjectRef, actor)
    return current_actor()


def selected_related_paths(
    model: type[models.Model],
    selected: Any,
    *,
    max_depth: int = 5,
) -> tuple[str, ...]:
    """Flatten Django's internal ``query.select_related`` shape into paths."""
    if selected is True:
        return tuple(_default_select_related_paths(model, max_depth=max_depth))
    if isinstance(selected, dict):
        paths: list[str] = []
        _collect_select_dict_paths(model, selected, prefix="", paths=paths)
        return tuple(dict.fromkeys(paths))
    return ()


def guard_selected_related_instances(
    instances: Iterable[Any],
    *,
    root_model: type[models.Model],
    paths: Sequence[str],
    actor: SubjectRef | None,
    mode: FieldDenyMode,
) -> None:
    """Batch-check selected to-one related objects before callers serialize them."""
    if not paths:
        return

    batches: dict[type[models.Model], dict[str, models.Model]] = {}
    for instance in instances:
        if not isinstance(instance, models.Model):
            continue
        for path in paths:
            _collect_cached_related(
                instance,
                root_model=root_model,
                path=path,
                batches=batches,
            )

    protected_batches = {
        model: rows for model, rows in batches.items() if model_resource_type(model)
    }
    if not protected_batches:
        return
    if actor is None:
        raise MissingActorError(
            "rebac_select_related() loaded REBAC-bound related objects but no actor "
            "is available to check them."
        )

    active_backend = backend()
    for model, rows_by_id in protected_batches.items():
        rebac_type = model_resource_type(model)
        if not rebac_type:
            continue
        action = str(getattr(model._meta, "rebac_default_action", "read"))
        rows = list(rows_by_id.values())

        if not backend_grants_all(
            active_backend,
            subject=actor,
            action=action,
            resource_type=rebac_type,
        ):
            allowed = set(
                accessible_ids(
                    active_backend,
                    subject=actor,
                    action=action,
                    resource_type=rebac_type,
                )
            )
            denied = sorted(set(rows_by_id) - allowed)
            if denied:
                sample = ", ".join(denied[:5])
                raise PermissionDenied(
                    f"select_related({model.__name__}) loaded {len(denied)} "
                    f"related row(s) outside actor scope (e.g. {sample})."
                )

        for row in rows:
            row._rebac_actor = actor  # type: ignore[attr-defined]
            row._rebac_field_deny = mode  # type: ignore[attr-defined]
        apply_field_visibility(rows, model=model, actor=actor, mode=mode)


def scope_prefetch_lookups(
    model: type[models.Model],
    lookups: Sequence[Any],
    *,
    actor: SubjectRef | None,
    mode: FieldDenyMode | None,
) -> tuple[Any, ...]:
    """Turn bare protected prefetches into actor-scoped ``Prefetch`` objects."""
    scoped: dict[str, Any] = {}
    for lookup in lookups:
        if lookup is None:
            scoped.clear()
            scoped[""] = lookup
            continue
        if isinstance(lookup, str):
            prefixes = protected_lookup_prefixes(model, lookup)
            if prefixes:
                for path, target in prefixes:
                    _remember_prefetch(
                        scoped,
                        Prefetch(
                            path,
                            queryset=_scoped_queryset_for_prefetch(
                                target,
                                queryset=None,
                                actor=actor,
                                mode=mode,
                            ),
                        ),
                    )
            else:
                _remember_prefetch(scoped, lookup)
            continue
        if isinstance(lookup, Prefetch):
            prefixes = protected_lookup_prefixes(model, lookup.prefetch_through)
            if prefixes:
                final_path = lookup.prefetch_through
                for path, target in prefixes:
                    if path == final_path:
                        copied = copy.copy(lookup)
                        copied.queryset = _scoped_queryset_for_prefetch(
                            target,
                            queryset=lookup.queryset,
                            actor=actor,
                            mode=mode,
                        )
                        _remember_prefetch(scoped, copied)
                    else:
                        _remember_prefetch(
                            scoped,
                            Prefetch(
                                path,
                                queryset=_scoped_queryset_for_prefetch(
                                    target,
                                    queryset=None,
                                    actor=actor,
                                    mode=mode,
                                ),
                            ),
                        )
            else:
                _remember_prefetch(scoped, lookup)
            continue
        _remember_prefetch(scoped, lookup)
    return tuple(scoped.values())


def protected_lookup_prefixes(
    model: type[models.Model],
    lookup: str,
) -> tuple[tuple[str, type[models.Model]], ...]:
    """Return protected relation prefixes along a lookup path."""
    current = model
    prefixes: list[tuple[str, type[models.Model]]] = []
    path_parts: list[str] = []
    for part in lookup.split("__"):
        if not part:
            return ()
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            return ()
        related = getattr(field, "related_model", None)
        if related is None:
            return ()
        current = cast(type[models.Model], related)
        path_parts.append(part)
        if model_resource_type(current):
            prefixes.append(("__".join(path_parts), current))
    return tuple(prefixes)


def _remember_prefetch(scoped: dict[str, Any], lookup: Any) -> None:
    if isinstance(lookup, Prefetch):
        key = lookup.prefetch_to
    elif isinstance(lookup, str):
        key = lookup
    else:
        key = f"#{len(scoped)}"
    existing = scoped.get(key)
    if existing is None or isinstance(existing, str):
        scoped[key] = lookup


def related_model_for_lookup(
    model: type[models.Model],
    lookup: str,
) -> type[models.Model] | None:
    """Resolve the final related model for a Django relation lookup path."""
    current = model
    for part in lookup.split("__"):
        if not part:
            return None
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            return None
        related = getattr(field, "related_model", None)
        if related is None:
            return None
        current = cast(type[models.Model], related)
    return current


def queryset_has_rebac_scope(queryset: Any) -> bool:
    return bool(
        getattr(queryset, "_rebac_actor", None) is not None
        or getattr(queryset, "_rebac_sudo_reason", None) is not None
    )


def _scoped_queryset_for_prefetch[M: models.Model](
    model: type[M],
    *,
    queryset: QuerySet[M] | None,
    actor: SubjectRef | None,
    mode: FieldDenyMode | None,
) -> QuerySet[M]:
    if queryset is None:
        queryset = model._default_manager.all()
    elif not hasattr(queryset, "with_actor") and model_resource_type(model):
        pk = model._meta.pk
        if pk is not None:
            base = model._default_manager.all()
            queryset = base.filter(**{f"{pk.attname}__in": queryset.values(pk.attname)})

    if actor is not None and not queryset_has_rebac_scope(queryset):
        with_actor = getattr(queryset, "with_actor", None)
        if callable(with_actor):
            queryset = with_actor(actor)
    if mode is not None and getattr(queryset, "_rebac_field_deny", None) is None:
        on_field_deny = getattr(queryset, "on_field_deny", None)
        if callable(on_field_deny):
            queryset = on_field_deny(mode)
    return _ensure_resource_id_loaded(queryset, model)


def _ensure_resource_id_loaded[M: models.Model](
    queryset: QuerySet[M],
    model: type[M],
) -> QuerySet[M]:
    if not model_resource_type(model):
        return queryset
    fields, defer = queryset.query.deferred_loading
    if defer or not fields:
        return queryset
    attr = _resource_id_attr(model)
    if "." in attr:
        return queryset
    wanted = set(fields)
    wanted.update(_resource_id_field_names(model, attr))
    return queryset.only(*wanted)


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


def _collect_select_dict_paths(
    model: type[models.Model],
    selected: dict[str, Any],
    *,
    prefix: str,
    paths: list[str],
) -> None:
    current_model = model
    for name, nested in selected.items():
        path = f"{prefix}__{name}" if prefix else name
        paths.append(path)
        target = related_model_for_lookup(current_model, name)
        if target is not None and isinstance(nested, dict) and nested:
            _collect_select_dict_paths(target, nested, prefix=path, paths=paths)


def _default_select_related_paths(
    model: type[models.Model],
    *,
    max_depth: int,
    prefix: str = "",
    seen: frozenset[type[models.Model]] = frozenset(),
) -> list[str]:
    if max_depth <= 0 or model in seen:
        return []
    paths: list[str] = []
    next_seen = seen | {model}
    for field in model._meta.get_fields():
        if getattr(field, "auto_created", False):
            continue
        if not getattr(field, "is_relation", False):
            continue
        if not (getattr(field, "many_to_one", False) or getattr(field, "one_to_one", False)):
            continue
        if getattr(field, "null", False):
            continue
        related = getattr(field, "related_model", None)
        if related is None:
            continue
        name = field.name
        path = f"{prefix}__{name}" if prefix else name
        paths.append(path)
        paths.extend(
            _default_select_related_paths(
                cast(type[models.Model], related),
                max_depth=max_depth - 1,
                prefix=path,
                seen=next_seen,
            )
        )
    return paths


def _collect_cached_related(
    instance: models.Model,
    *,
    root_model: type[models.Model],
    path: str,
    batches: dict[type[models.Model], dict[str, models.Model]],
) -> None:
    current: models.Model | None = instance
    current_model = root_model
    for part in path.split("__"):
        if current is None:
            return
        target = related_model_for_lookup(current_model, part)
        if target is None:
            return
        cache = getattr(current._state, "fields_cache", {})
        if part not in cache:
            return
        related = cache[part]
        if related is None or not isinstance(related, models.Model):
            return
        rebac_type = model_resource_type(type(related))
        if rebac_type:
            resource_id = str(getattr(related, _resource_id_attr(type(related))))
            batches.setdefault(type(related), {})[resource_id] = related
        current = related
        current_model = type(related)


def _resource_id_attr(model: type[models.Model]) -> str:
    from ._id import resource_id_attr

    return resource_id_attr(model)
