"""Evaluate field-backing filters using established candidate and database facts."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import QuerySet, Value
from django.db.models.expressions import BaseExpression, Combinable
from django.db.models.functions import Cast


def _forward_lookup(
    model: type[models.Model], lookup: str
) -> tuple[models.Field[Any, Any], int] | None:
    """Locate the scalar lookup owner, rejecting traversals changed by insert."""

    parts = lookup.split("__")
    for index, part in enumerate(parts):
        field = model._meta.pk if part == "pk" else model._meta.get_field(part)
        if not isinstance(field, models.Field) or not field.concrete:
            return None
        if not field.is_relation:
            return field, index + 1
        if not isinstance(field, (models.ForeignKey, models.OneToOneField)):
            return None
        if part == field.attname or index == len(parts) - 1:
            return field.target_field, index + 1
        model = field.related_model
        try:
            model._meta.pk if parts[index + 1] == "pk" else model._meta.get_field(parts[index + 1])
        except FieldDoesNotExist:
            # Remaining components are Django transforms/lookups on this FK.
            return field.target_field, index + 1
    raise ValueError("A candidate filter must identify a model field.")


def _value_is_unresolved(field: models.Field[Any, Any], value: Any) -> bool:
    return (
        isinstance(value, (BaseExpression, Combinable))
        or field.generated
        or bool(getattr(field, "auto_now", False))
        or bool(getattr(field, "auto_now_add", False))
        or (field.primary_key and value is None)
    )


def _filter_candidate_targets(
    instance: models.Model,
    first_field: models.ForeignKey[Any, Any],
    targets: QuerySet[Any],
    filters: dict[str, Any],
    *,
    using: str | None,
) -> QuerySet[Any] | None:
    """Filter already-resolved first-hop targets on the candidate's write alias.

    Target predicates retain their native Django joins. Candidate scalar values
    become typed SQL annotations, and independent forward-FK predicates are
    grouped on their persisted target. ``None`` means an insert-dependent fact
    prevents a truthful projection; it never means an empty target set.
    """

    predicates: dict[str, Any] = {}
    annotations: dict[str, BaseExpression] = {}
    other_fields: dict[str, models.ForeignKey[Any, Any]] = {}
    other_filters: dict[str, dict[str, Any]] = {}
    unavailable_aliases = {field.name for field in targets.model._meta.get_fields()} | set(
        targets.query.annotations
    )

    def annotate(value: Any, field: models.Field[Any, Any], suffix: str, expected: Any) -> None:
        alias = f"_rebac_candidate_filter_{len(annotations)}"
        while alias in unavailable_aliases:
            alias += "_"
        unavailable_aliases.add(alias)
        # Python None is saved as SQL NULL. In particular, typing Value(None)
        # as JSONField would instead encode JSON null and change membership.
        annotations[alias] = (
            Cast(Value(None), output_field=field)
            if value is None
            else Value(value, output_field=field)
        )
        predicates[f"{alias}__{suffix}" if suffix else alias] = expected

    for lookup, expected in sorted(filters.items()):
        resolved = _forward_lookup(type(instance), lookup)
        if resolved is None:
            return None
        scalar_field, consumed = resolved
        parts = lookup.split("__")
        root = parts[0]
        source_field = instance._meta.pk if root == "pk" else instance._meta.get_field(root)
        if not isinstance(source_field, models.Field):
            return None
        if source_field.generated:
            return None
        if consumed == 1:
            value = getattr(instance, source_field.attname)
            if _value_is_unresolved(source_field, value):
                return None
            annotate(value, scalar_field, "__".join(parts[1:]), expected)
            continue
        if not isinstance(source_field, (models.ForeignKey, models.OneToOneField)):
            return None
        if source_field is first_field:
            predicates["__".join(parts[1:])] = expected
            continue
        value = getattr(instance, source_field.attname)
        if _value_is_unresolved(source_field, value):
            return None
        if value is None:
            # A missing forward target contributes SQL NULL to every terminal
            # column, including lookups such as related__name__isnull=True.
            annotate(None, scalar_field, "__".join(parts[consumed:]), expected)
            continue
        other_fields[source_field.name] = source_field
        other_filters.setdefault(source_field.name, {})["__".join(parts[1:])] = expected

    filtered: QuerySet[Any] = targets.alias(**annotations).filter(**predicates)
    for name, field in sorted(other_fields.items()):
        prepared = field.target_field.get_prep_value(getattr(instance, field.attname))
        if isinstance(prepared, (BaseExpression, Combinable)):
            raise ValueError("The proposed foreign-key identity did not resolve to a scalar value.")
        related = field.related_model._base_manager.db_manager(using).filter(
            **{field.target_field.name: prepared}
        )
        if not related.exists():
            raise ValueError("The proposed related object is unavailable on the write alias.")
        if not related.filter(**other_filters[name]).exists():
            filtered = filtered.none()
    return filtered
