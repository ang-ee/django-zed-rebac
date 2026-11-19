"""Fields a row shows to whoever reads a row that refers to it (proposal 0020).

A list of messages shows each author's name. Today ``rebac_select_related``
joins the author rows and then asks, for the batch, whether the actor may read
each one, and raises if it may not: the list cannot be shown to someone who may
read the messages and not the accounts. Most applications mean something
narrower: an account's name is visible wherever the account is referred to,
and the account itself is not.

A model says so by declaring display fields

    class Account(RebacMixin, models.Model):
        class Meta:
            rebac_resource_type = "app/account"
            rebac_display_fields = ("name", "avatar")

or, for a model that cannot carry the option, ``display.declare(Model, "name")``.
With ``REBAC_DISPLAY_FIELDS`` set, a related row that ``rebac_select_related``
loaded is then delivered with its key and its display fields, every other field
hidden as a denied field is (``redacted_fields`` names them), in one of two ways:

- ``"fallback"``: the batch is checked as before. A row the actor holds is
  delivered whole; a row it does not hold is delivered with display fields
  instead of raising.
- ``True``: a row of a model that declares display fields is not checked at
  all, and is always delivered with display fields only. No permission
  statement is issued for the relation. Read the row itself to see more.

A model that declares none behaves as before in both. A display field that
carries a field gate (``read__<field>``) is not displayed: the gate decides it,
under whatever policy is current. A hidden foreign key reads as absent, so the
rows that hung off it are no longer reached from the display row; the guard
checks what is still reached.

Only a ``RebacMixin`` model can declare display fields: its ``save`` leaves
hidden fields alone, where a plain model's would write them back as NULL.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models

from ._id import resource_id_attr
from .types import FieldDenyMode, SubjectRef

_declared: dict[type[models.Model], tuple[str, ...]] = {}
# Declarations already checked against their model.
_checked: set[tuple[type[models.Model], tuple[str, ...]]] = set()


def mode() -> str:
    """``"always"``, ``"fallback"`` or empty for off."""
    from .conf import app_settings

    value = app_settings.REBAC_DISPLAY_FIELDS
    if value is True or value == "always":
        return "always"
    return "fallback" if value == "fallback" else ""


def declare(model: type[models.Model], *fields: str) -> None:
    """Declare ``model``'s display fields, for a model whose ``Meta`` cannot be edited.

    Declaring none withdraws an earlier declaration.
    """
    if fields:
        _declared[model] = tuple(fields)
    else:
        _declared.pop(model, None)


def _declaration(model: type[models.Model]) -> tuple[str, ...]:
    found = _declared.get(model)
    if found is None:
        found = tuple(getattr(model._meta, "rebac_display_fields", ()) or ())
    return found


def fields(model: type[models.Model]) -> frozenset[str]:
    """The fields of ``model`` shown on a display row; empty when it declares none."""
    return _sides(model)[0]


def _sides(model: type[models.Model]) -> tuple[frozenset[str], frozenset[str]]:
    """``(shown, hidden)`` field names of ``model``'s display rows, under the current policy."""
    declared = _declaration(model)
    if not declared:
        return frozenset(), frozenset()
    from .field_visibility import gated_read_fields
    from .mixins import RebacMixin

    concrete = {field.name: field for field in model._meta.concrete_fields}
    if (model, declared) not in _checked:
        if not issubclass(model, RebacMixin):
            raise ImproperlyConfigured(
                f"{model.__name__}: display fields need a RebacMixin model, whose save() "
                "leaves hidden fields alone."
            )
        for name in declared:
            try:
                field = model._meta.get_field(name)
            except FieldDoesNotExist as exc:
                raise ImproperlyConfigured(
                    f"{model.__name__}: display field {name!r} is not a field of the model."
                ) from exc
            if field.name not in concrete:
                raise ImproperlyConfigured(
                    f"{model.__name__}: display field {name!r} is not one of the model's "
                    "own columns."
                )
        _checked.add((model, declared))
    # A gated field is decided by its gate, never by this declaration: read from the
    # policy each time, since a later policy may gate a field this one does not.
    shown = frozenset(declared) - gated_read_fields(model)
    identity = {resource_id_attr(model)}
    hidden = frozenset(
        name
        for name, field in concrete.items()
        if not field.primary_key and name not in shown and name not in identity
    )
    return shown, hidden


def deliver(
    rows: Iterable[models.Model],
    *,
    model: type[models.Model],
    actor: SubjectRef,
    field_mode: FieldDenyMode,
) -> None:
    """Make ``rows`` display rows: their key and display fields, the rest hidden.

    A hidden column is set as a denied field is. A hidden foreign key is set
    through its column, which drops the related object this row had loaded and
    touches nothing on that object: a one-to-one assigned through its
    descriptor would also clear the other side's link back to this row.
    """
    from .field_visibility import (
        _remember_resource_id,
        _scrub_loaded_value,
        mark_denied_fields,
        redacted_fields,
        runtime_field_deny_mode,
    )

    _shown, hidden = _sides(model)
    runtime = runtime_field_deny_mode(field_mode)
    denied: FieldDenyMode = "omit" if runtime == "omit" else "redact"
    relations = {
        name: model._meta.get_field(name)
        for name in hidden
        if model._meta.get_field(name).is_relation
    }
    columns = hidden - relations.keys()
    for row in rows:
        row._rebac_actor = actor  # type: ignore[attr-defined]
        row._rebac_field_deny = field_mode  # type: ignore[attr-defined]
        mark_denied_fields(row, columns, mode=denied)
        if relations:
            _remember_resource_id(row)
            for name, field in relations.items():
                setattr(row, cast(Any, field).attname, None)
                _scrub_loaded_value(row, name)
            row._rebac_redacted_fields = redacted_fields(row) | relations.keys()  # type: ignore[attr-defined]
            if denied == "omit":
                omitted: frozenset[str] = getattr(row, "_rebac_omitted_fields", None) or frozenset()
                row._rebac_omitted_fields = frozenset(omitted) | relations.keys()  # type: ignore[attr-defined]
        row._rebac_display_only = True  # type: ignore[attr-defined]
        # Nothing is left for the field gates to decide on this row.
        row._rebac_visibility_marker = (actor, runtime)  # type: ignore[attr-defined]


def is_display_row(instance: models.Model) -> bool:
    """Whether ``instance`` was delivered with its display fields only."""
    return bool(getattr(instance, "_rebac_display_only", False))
