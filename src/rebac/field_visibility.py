"""Field-level read visibility helpers for ``read__<field>`` gates."""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from django.db import models

from ._id import resource_id_attr
from .conf import app_settings
from .resources import model_resource_type
from .schema.ast import Schema
from .schema.walker import field_gated_actions
from .types import CheckResult, FieldDenyMode, ObjectRef, PermissionResult, SubjectRef

if TYPE_CHECKING:  # pragma: no cover
    from .backends.base import Backend


FIELD_DENY_MODES = frozenset({"allow", "redact", "omit", "raise"})


def validate_field_deny_mode(mode: Any) -> FieldDenyMode:
    if mode not in FIELD_DENY_MODES:
        expected = ", ".join(sorted(FIELD_DENY_MODES))
        raise ValueError(f"Unknown field read deny mode {mode!r}; expected one of {expected}")
    return cast(FieldDenyMode, mode)


def effective_field_deny_mode(override: FieldDenyMode | None = None) -> FieldDenyMode:
    raw = override if override is not None else app_settings.REBAC_FIELD_READ_MODE
    return validate_field_deny_mode(raw)


def runtime_field_deny_mode(mode: FieldDenyMode) -> FieldDenyMode:
    """Mode the descriptor-free 0.7 engine can actually apply."""
    if mode == "raise":
        return "redact"
    return mode


def warn_raise_mode_degrades(*, stacklevel: int = 2) -> None:
    warnings.warn(
        "rebac.W008: field read deny mode 'raise' is reserved for the "
        "descriptor-based protected-fields tier and currently degrades to 'redact'.",
        RuntimeWarning,
        stacklevel=stacklevel,
    )


def backend_schema() -> Schema | None:
    """Best-effort schema accessor shared by read and write field gates."""
    from .backends import backend

    accessor = getattr(backend(), "schema", None)
    if not callable(accessor):
        return None
    try:
        result = accessor()
    except Exception:
        return None
    return result if isinstance(result, Schema) else None


def backend_grants_all(
    active_backend: Backend,
    *,
    subject: SubjectRef,
    action: str,
    resource_type: str,
) -> bool:
    grants_all = getattr(active_backend, "grants_all", None)
    if not callable(grants_all):
        return False
    return bool(grants_all(subject=subject, action=action, resource_type=resource_type))


def accessible_ids(
    active_backend: Backend,
    *,
    subject: SubjectRef,
    action: str,
    resource_type: str,
) -> tuple[str, ...]:
    """Route ``accessible`` through the ambient evaluator when one exists."""
    from .evaluator import current_evaluator

    evaluator = current_evaluator()
    if evaluator is None:
        return tuple(
            str(v)
            for v in active_backend.accessible(
                subject=subject,
                action=action,
                resource_type=resource_type,
            )
        )
    return tuple(
        str(v)
        for v in evaluator.accessible(
            active_backend,
            subject=subject,
            action=action,
            resource_type=resource_type,
        )
    )


def check_field_access(
    active_backend: Backend,
    *,
    subject: SubjectRef,
    action: str,
    resource: ObjectRef,
    context: dict[str, Any] | None = None,
) -> CheckResult:
    """Route ``check_access`` through the ambient evaluator when one exists."""
    from .evaluator import current_evaluator

    evaluator = current_evaluator()
    if evaluator is None:
        return active_backend.check_access(
            subject=subject,
            action=action,
            resource=resource,
            context=context,
        )
    return evaluator.check(
        active_backend,
        subject=subject,
        action=action,
        resource=resource,
        context=context,
    )


def gated_read_fields(model: type[models.Model]) -> frozenset[str]:
    """Field names on ``model`` protected by declared ``read__<field>`` permissions."""
    rebac_type = model_resource_type(model)
    if not rebac_type:
        return frozenset()
    schema = backend_schema()
    if schema is None:
        return frozenset()
    definition = schema.get_definition(rebac_type)
    if definition is None:
        return frozenset()
    prefix = "read__"
    fields: set[str] = set()
    for action in field_gated_actions(definition, "read"):
        field_name = _model_field_name(model, action.removeprefix(prefix))
        if field_name is not None:
            fields.add(field_name)
    return frozenset(fields)


def visible_id_sets(
    *,
    model: type[models.Model],
    actor: SubjectRef,
    fields: frozenset[str],
    active_backend: Backend | None = None,
) -> dict[str, frozenset[str] | None]:
    """Map each gated field to visible resource ids.

    ``None`` means the field action grants every row of this resource type, so
    callers should skip per-row redaction for that field.
    """
    rebac_type = model_resource_type(model)
    if not rebac_type or not fields:
        return {}
    if active_backend is None:
        from .backends import backend

        active_backend = backend()
    visible: dict[str, frozenset[str] | None] = {}
    for field_name in fields:
        action = f"read__{field_name}"
        if backend_grants_all(
            active_backend,
            subject=actor,
            action=action,
            resource_type=rebac_type,
        ):
            visible[field_name] = None
            continue
        visible[field_name] = frozenset(
            accessible_ids(
                active_backend,
                subject=actor,
                action=action,
                resource_type=rebac_type,
            )
        )
    return visible


def apply_field_visibility(
    instances: Iterable[Any],
    *,
    model: type[models.Model],
    actor: SubjectRef,
    mode: FieldDenyMode,
) -> None:
    """Redact/omit denied ``read__<field>`` values on a fetched batch."""
    runtime_mode = runtime_field_deny_mode(validate_field_deny_mode(mode))
    if runtime_mode == "allow":
        return
    batch = [inst for inst in instances if isinstance(inst, models.Model)]
    if not batch:
        return
    fields = gated_read_fields(model)
    if not fields:
        return

    from .backends import backend

    active_backend = backend()
    visible = visible_id_sets(
        model=model,
        actor=actor,
        fields=fields,
        active_backend=active_backend,
    )
    rebac_type = model_resource_type(model)
    if not rebac_type:
        return

    for inst in batch:
        denied: set[str] = set()
        resource_id = _instance_resource_id(inst, model)
        for field_name in fields:
            ids = visible.get(field_name)
            if ids is None or resource_id in ids:
                continue
            if _conditional_field_is_visible(
                active_backend,
                actor=actor,
                action=f"read__{field_name}",
                resource=ObjectRef(rebac_type, resource_id),
            ):
                continue
            denied.add(field_name)
        mark_denied_fields(inst, denied, mode=runtime_mode)


def mark_denied_fields(
    instance: models.Model,
    fields: Iterable[str],
    *,
    mode: FieldDenyMode,
) -> None:
    field_set = frozenset(fields)
    if not field_set:
        return
    _remember_resource_id(instance)
    for field_name in field_set:
        setattr(instance, field_name, None)
        _scrub_loaded_value(instance, field_name)
    redacted = set(getattr(instance, "_rebac_redacted_fields", frozenset()) or frozenset())
    redacted.update(field_set)
    instance._rebac_redacted_fields = frozenset(redacted)  # type: ignore[attr-defined]
    if mode == "omit":
        omitted = set(getattr(instance, "_rebac_omitted_fields", frozenset()) or frozenset())
        omitted.update(field_set)
        instance._rebac_omitted_fields = frozenset(omitted)  # type: ignore[attr-defined]


def _model_field_name(model: type[models.Model], name: str) -> str | None:
    if name == "pk":
        pk = model._meta.pk
        return pk.name if pk is not None else None
    try:
        field = model._meta.get_field(name)
    except Exception:
        for candidate in model._meta.concrete_fields:
            if getattr(candidate, "attname", None) == name:
                return candidate.name
        return None
    if not getattr(field, "concrete", False):
        return None
    return field.name


def projection_field_names(
    model: type[models.Model],
    raw_fields: Iterable[Any] | None,
) -> frozenset[str] | None:
    """Normalise a values()/values_list() field projection to model field names.

    ``None`` means this is not a projection queryset. An empty iterable means
    ``values()`` with no explicit field list, which projects every concrete
    field on the model.
    """
    if raw_fields is None:
        return None
    items = tuple(raw_fields)
    if not items:
        return frozenset(f.name for f in model._meta.concrete_fields)
    names: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        local_name = item.split("__", 1)[0]
        field_name = _model_field_name(model, local_name)
        if field_name is not None:
            names.add(field_name)
    return frozenset(names)


def _instance_resource_id(instance: models.Model, model: type[models.Model]) -> str:
    return str(getattr(instance, resource_id_attr(model)))


def _remember_resource_id(instance: models.Model) -> None:
    if getattr(instance, "_rebac_resource_id", None) is not None:
        return
    try:
        instance._rebac_resource_id = _instance_resource_id(instance, type(instance))  # type: ignore[attr-defined]
    except Exception:
        return


def _scrub_loaded_value(instance: models.Model, field_name: str) -> None:
    loaded: dict[str, Any] | None = getattr(instance, "_rebac_loaded_values", None)
    if loaded is None:
        return
    loaded.pop(field_name, None)
    try:
        field = type(instance)._meta.get_field(field_name)
    except Exception:
        return
    loaded.pop(getattr(field, "attname", field_name), None)


def _conditional_field_is_visible(
    active_backend: Backend,
    *,
    actor: SubjectRef,
    action: str,
    resource: ObjectRef,
) -> bool:
    if app_settings.REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL:
        return False
    result = check_field_access(
        active_backend,
        subject=actor,
        action=action,
        resource=resource,
    )
    return result.allowed or result.result is PermissionResult.CONDITIONAL_PERMISSION
