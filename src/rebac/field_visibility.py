"""Field-level read visibility helpers for ``read__<field>`` gates."""

from __future__ import annotations

import warnings
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast

from django.core.exceptions import ImproperlyConfigured
from django.db import models, router

from ._id import resource_id_attr
from .conf import app_settings
from .resources import model_resource_type
from .schema.ast import Schema
from .schema.walker import field_gated_actions
from .types import CheckResult, FieldDenyMode, ObjectRef, PermissionResult, SubjectRef

if TYPE_CHECKING:  # pragma: no cover
    from .backends.local import LocalBackend


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


def backend_schema() -> Schema:
    """The active backend's installed schema, shared by the read and write field gates.

    The gates decide from the schema alone, so they never run without it: a
    backend whose ``schema()`` is missing or returns something other than a
    :class:`Schema` is a configuration error, and an exception ``schema()``
    raises propagates to the read or write that needed it.
    """
    from .backends import backend

    active_backend = backend()
    accessor = getattr(active_backend, "schema", None)
    if not callable(accessor):
        raise ImproperlyConfigured(
            f"{type(active_backend).__qualname__} has no callable schema(); the "
            "read__<field> and write__<field> gates read the installed schema through it."
        )
    result = accessor()
    if not isinstance(result, Schema):
        raise ImproperlyConfigured(
            f"{type(active_backend).__qualname__}.schema() returned "
            f"{type(result).__qualname__}, not a rebac.schema.ast.Schema; the "
            "read__<field> and write__<field> gates read the installed schema through it."
        )
    return result


def held_ids(
    active_backend: LocalBackend,
    *,
    subject: SubjectRef,
    action: str,
    model: type[models.Model],
    resource_ids: Iterable[str],
    using: str | None = None,
) -> tuple[str, ...]:
    """The supplied candidate ids on which ``subject`` holds ``action``.

    ``using`` is the alias the caller's statement runs on; a read defaults to
    the model's read alias.
    """
    resource_type = model_resource_type(model)
    if resource_type is None:
        return ()
    ids = tuple(dict.fromkeys(resource_ids))
    if not ids:
        return ()
    allowed = active_backend.held(
        subject=subject,
        action=action,
        resource_type=resource_type,
        ids=ids,
        using=using or router.db_for_read(model),
    )
    return tuple(value for value in ids if value in allowed)


def check_field_access(
    active_backend: LocalBackend,
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
    definition = backend_schema().get_definition(rebac_type)
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
    resource_ids: Iterable[str],
    active_backend: LocalBackend | None = None,
) -> dict[str, frozenset[str]]:
    """Map each gated field to its visible subset of the supplied row ids.

    Every field of the batch is decided together: one statement per distinct
    permission, memoised in the ambient evaluator scope when eligible.
    """
    rebac_type = model_resource_type(model)
    if not rebac_type or not fields:
        return {}
    if active_backend is None:
        from .backends import backend

        active_backend = backend()
    from .evaluator import current_evaluator

    candidates = tuple(dict.fromkeys(resource_ids))
    actions = tuple(f"read__{name}" for name in sorted(fields))
    using = router.db_for_read(model)
    evaluator = current_evaluator()
    if evaluator is None:
        answers = active_backend.held_many(
            subject=actor,
            actions=actions,
            resource_type=rebac_type,
            ids=candidates,
            using=using,
        )
    else:
        answers = evaluator._held_many(
            active_backend,
            subject=actor,
            model=model,
            resource_type=rebac_type,
            actions=actions,
            ids=candidates,
            using=using,
        )
    return {name: frozenset(answers[f"read__{name}"]) for name in fields}


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
    marker = (actor, runtime_mode)
    batch = [
        inst
        for inst in instances
        if isinstance(inst, models.Model)
        and getattr(inst, "_rebac_visibility_marker", None) != marker
    ]
    if not batch:
        return
    fields = gated_read_fields(model)
    if not fields:
        return

    from .backends import backend

    active_backend = backend()
    rebac_type = model_resource_type(model)
    if not rebac_type:
        return
    resource_ids = [_instance_resource_id(inst) for inst in batch]
    visible = visible_id_sets(
        model=model,
        actor=actor,
        fields=fields,
        resource_ids=resource_ids,
        active_backend=active_backend,
    )

    for inst, resource_id in zip(batch, resource_ids, strict=True):
        denied: set[str] = set()
        for field_name in fields:
            if resource_id in visible[field_name]:
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
        cast(Any, inst)._rebac_visibility_marker = marker


def redacted_fields(instance: models.Model) -> frozenset[str]:
    """Names hidden by field-read enforcement on this loaded instance.

    Unmarked models have no redacted fields. The immutable result describes the
    fetched instance, without evaluating permissions or issuing a query.
    """
    return frozenset(getattr(instance, "_rebac_redacted_fields", frozenset()) or frozenset())


def refuse_hidden_fields(
    model: type[models.Model],
    rows: Iterable[Any],
    fields: Iterable[str],
    *,
    operation: str,
    reads: bool = False,
    assigned: bool = False,
    inserted: bool = False,
) -> None:
    """Refuse ``operation`` on ``fields`` of ``rows`` when one of them is hidden on a row.

    A hidden field is one ``redacted_fields`` names on the row: a ``read__<field>``
    its reader was denied, or a field a display row does not show. The row holds
    ``None`` there in place of the stored value: writing the field from it would
    store ``None``, and reading it again (``reads``) would read what was hidden.
    ``bulk_update()``, ``save(update_fields=...)`` and
    ``refresh_from_db(fields=...)`` name theirs and are refused here, under any
    actor or bypass; the bulk ones and the refresh before any statement. A save
    that names no fields leaves out a hidden field that still holds its
    placeholder, and refuses here, before any statement, one given another value
    (``assigned``), which it would not write. An insert of the row (``inserted``:
    a save that would insert it, ``bulk_create()``, ``refuse_copies``) is refused
    here before any statement, as it would store the placeholders as data.
    ``fields`` are names or attnames
    (Django's ``get_field`` reads both; the record holds names); one that is not
    a field of ``model`` is left to Django to refuse.
    """
    from django.core.exceptions import FieldDoesNotExist

    from .errors import PermissionDenied

    named: set[str] = set()
    for name in fields:
        try:
            named.add(model._meta.get_field(name).name)
        except FieldDoesNotExist:
            continue
    if not named:
        return
    for row in rows:
        hidden = sorted(named & redacted_fields(row))
        if not hidden:
            continue
        key = row.pk
        if inserted and model._meta.pk is not None:
            # The row as it was read: a copy's own key may be None or another one.
            loaded = getattr(row, "_rebac_loaded_values", None) or {}
            key = loaded.get(model._meta.pk.attname, key)
        found = (
            f"{operation} refused: {model._meta.label} row {key!r} has {', '.join(hidden)} "
            "hidden (redacted_fields: a read__<field> its reader was denied, or a field a "
            "display row does not show)"
        )
        if inserted:
            raise PermissionDenied(
                f"{found}, and {operation} would insert it as a new row, storing the None it holds "
                "there as data. A row with hidden fields is not copied by any insert: build the "
                "new row from values of your own, or from a row read whole (for_write() on a "
                "scoped read; a display row read again by a scoped read of its model)."
            )
        if assigned:
            raise PermissionDenied(
                f"{found}, and holds a value given to it after it was hidden. A save that names "
                "no fields writes no hidden field, so the value would not be written. Write it "
                "from a row read for writing, or with QuerySet.update()."
            )
        effect = "read what was hidden" if reads else "store None over the stored value"
        raise PermissionDenied(
            f"{found}. The row holds None there in place of the stored value, and "
            f"{operation} would {effect}. Name only the fields the row shows."
        )


_ABSENT = object()
# The row's own record of the values the database returned for it.
_RECORDED = object()


def refuse_copies(model: type[models.Model], rows: Iterable[Any], *, operation: str) -> None:
    """Refuse ``operation``, which inserts ``rows``, when one of them has a hidden field.

    Such a row holds placeholders in its hidden fields; inserting it stores them as
    data. ``bulk_create()`` and ``abulk_create()`` through any manager, in every
    form (``ignore_conflicts``, ``update_conflicts``), are refused here before any
    statement. A row the caller built (``Model(**values)``) carries no record.
    """
    copied = [row for row in rows if redacted_fields(row)]
    if copied:
        names = [field.name for field in model._meta.concrete_fields]
        refuse_hidden_fields(model, copied, names, operation=operation, inserted=True)


def mark_denied_fields(
    instance: models.Model,
    fields: Iterable[str],
    *,
    mode: FieldDenyMode,
    returned: Mapping[str, Any] | None | object = _RECORDED,
) -> None:
    """Hide ``fields`` on ``instance``: each is set to ``None`` and recorded as hidden.

    Each field is hidden through ``setattr``, so its descriptor and the model's own
    ``__setattr__`` run as for any assignment. Every shown column the hide changed
    (an ``ImageField``'s descriptor sets its width and height) is then put back as
    ``returned`` gives it: the values the database returned for the row (by default
    the row's ``_rebac_loaded_values``), never what model code derived while Django
    built the row, from a hidden value among others. A column the database did not
    return (deferred) is left deferred, so reading it loads the stored value.
    ``returned=None``, or a row without the record (built by hand, unpickled), puts
    back what the column held before the hide: ``refresh_from_db()`` hides again
    after reading the shown fields itself, and assigns no hidden one.
    """
    field_set = frozenset(fields)
    if not field_set:
        return
    _remember_resource_id(instance)
    if returned is _RECORDED:
        returned = getattr(instance, "_rebac_loaded_values", None)
    shown = _shown_columns(instance, field_set | redacted_fields(instance))
    for field_name in field_set:
        setattr(instance, field_name, None)
        _scrub_loaded_value(instance, field_name)
    held = vars(instance)
    for attname, before in shown.items():
        if held.get(attname, _ABSENT) is before:
            continue
        value = (
            before if returned is None else cast(Mapping[str, Any], returned).get(attname, _ABSENT)
        )
        if value is _ABSENT:
            held.pop(attname, None)
        else:
            held[attname] = value
    redacted = set(redacted_fields(instance))
    redacted.update(field_set)
    instance._rebac_redacted_fields = frozenset(redacted)  # type: ignore[attr-defined]
    if mode == "omit":
        omitted = set(getattr(instance, "_rebac_omitted_fields", frozenset()) or frozenset())
        omitted.update(field_set)
        instance._rebac_omitted_fields = frozenset(omitted)  # type: ignore[attr-defined]


def _shown_columns(instance: models.Model, hidden: frozenset[str]) -> dict[str, Any]:
    """What the row's shown columns hold, by attname; ``_ABSENT`` for a deferred one."""
    held = vars(instance)
    return {
        field.attname: held.get(field.attname, _ABSENT)
        for field in type(instance)._meta.concrete_fields
        if field.name not in hidden and field.attname not in hidden
    }


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


def _instance_resource_id(instance: models.Model) -> str:
    return str(getattr(instance, resource_id_attr(instance)))


def _remember_resource_id(instance: models.Model) -> None:
    if getattr(instance, "_rebac_resource_id", None) is not None:
        return
    try:
        instance._rebac_resource_id = _instance_resource_id(instance)  # type: ignore[attr-defined]
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
    active_backend: LocalBackend,
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
