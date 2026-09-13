"""Synthetic fields whose public values differ from their SQL storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models.expressions import BaseExpression

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
