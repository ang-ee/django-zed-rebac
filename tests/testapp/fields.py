"""Synthetic fields whose public values differ from their SQL storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models.expressions import BaseExpression, Col

if TYPE_CHECKING:
    _IntegerField = models.BigIntegerField[int | str | None, str | None]
else:
    _IntegerField = models.BigIntegerField


if TYPE_CHECKING:
    _CharField = models.CharField[str | None, str]
else:
    _CharField = models.CharField


class LowercaseCharField(_CharField):
    """A text column whose Python conversion is not the identity.

    Exercises the rule that only stock text/integer conversions may be
    compiled into a correlated SQL comparison; anything else must fall back to
    Python evaluation so every read path agrees.
    """

    def to_python(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).lower()

    def get_prep_value(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).lower()


class EncodedIntegerField(_IntegerField):
    """Store an integer while exposing an opaque, reversible public identity."""

    def from_db_value(
        self, value: Any, expression: BaseExpression, connection: BaseDatabaseWrapper
    ) -> str | None:
        return self.to_python(value)

    def to_python(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and value.startswith("item-"):
            return value
        return f"item-{int(value)}"

    def get_prep_value(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, str) and value.startswith("item-"):
            value = value.removeprefix("item-")
        return int(value)


class _VirtualEncodedIdentityDescriptor:
    def __init__(self, field: VirtualEncodedIdentityField) -> None:
        self.field = field

    def __get__(self, instance: models.Model | None, owner: type[models.Model]) -> Any:
        if instance is None:
            return self
        return self.field.to_python(instance.pk)


class VirtualEncodedIdentityField(EncodedIntegerField):
    """A queryable public identity projected from the model's existing PK column."""

    def contribute_to_class(
        self, cls: type[models.Model], name: str, private_only: bool = False, **kwargs: Any
    ) -> None:
        super().contribute_to_class(cls, name, private_only=True, **kwargs)
        self.concrete = False
        setattr(cls, self.attname, _VirtualEncodedIdentityDescriptor(self))

    def get_col(self, alias: str, output_field: models.Field[Any, Any] | None = None) -> Col:
        return Col(alias, self.model._meta.pk, output_field or self)


class ColumnlessIdentityField(EncodedIntegerField):
    """Malformed virtual identity retaining the stock column expression."""

    def contribute_to_class(
        self, cls: type[models.Model], name: str, private_only: bool = False, **kwargs: Any
    ) -> None:
        super().contribute_to_class(cls, name, private_only=True, **kwargs)
        self.column = None
        self.concrete = False


class NonExpressionIdentityField(VirtualEncodedIdentityField):
    """Malformed identity whose advertised ORM column is not an expression."""

    def get_col(self, alias: str, output_field: models.Field[Any, Any] | None = None) -> Any:
        return "not-an-expression"


class MissingLookupIdentityField(VirtualEncodedIdentityField):
    """Malformed identity without the lookups live backing requires."""

    def get_lookup(self, lookup_name: str) -> Any:
        if lookup_name in {"exact", "in"}:
            return None
        return super().get_lookup(lookup_name)
