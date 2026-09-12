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
