"""Lossless identity conversion using Django expressions, with guarded casts.

Invalid wire values produce SQL NULL, never a coerced identity. In particular,
the reserved '*' type-level ID cannot reach an integer or UUID cast.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import DEFAULT_DB_ALIAS, connections, models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Case, Expression, F, Func, Q, Value, When
from django.db.models.functions import Cast, Concat, Length, Lower, Replace, Substr
from django.db.models.lookups import Exact, GreaterThan, LessThan, LessThanOrEqual, Regex
from django.db.models.sql.compiler import SQLCompiler

from rebac._id import model_identity_fields, resource_id_attr
from rebac.errors import SchemaError

_INTEGER = r"^(0|[1-9][0-9]*|-[1-9][0-9]*)$"
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class Codec(Protocol):
    def to_wire(self, expr: Expression | str) -> Expression: ...
    def to_column(self, expr: Expression | str) -> Expression: ...
    def wire(self, value: Any, *, using: str = DEFAULT_DB_ALIAS) -> str: ...
    def is_canonical(self, wire: str, *, using: str = DEFAULT_DB_ALIAS) -> bool: ...


def _digits_at_most(expr: Expression, bound: int) -> Q:
    text = str(bound)
    return Q(LessThan(Length(expr), Value(len(text)))) | (
        Q(Exact(Length(expr), Value(len(text)))) & Q(LessThanOrEqual(expr, Value(text)))
    )


@dataclass(frozen=True)
class _IdentityCodec:
    field: models.Field[Any, Any]
    kind: str

    @property
    def max_length(self) -> int:
        return min(64, self.field.max_length or 64)

    def is_canonical(self, wire: str, *, using: str = DEFAULT_DB_ALIAS) -> bool:
        if not isinstance(wire, str) or not wire or len(wire) > 64 or wire == "*":
            return False
        try:
            value = self.field.to_python(wire)
        except ValidationError, ValueError, TypeError:
            return False
        if str(value) != wire:
            return False
        if self.kind == "integer":
            minimum, maximum = connections[using].ops.integer_field_range(
                self.field.get_internal_type()
            )
            number = int(value)
            return (minimum is None or number >= minimum) and (maximum is None or number <= maximum)
        return self.kind != "text" or len(wire) <= self.max_length

    def wire(self, value: Any, *, using: str = DEFAULT_DB_ALIAS) -> str:
        if value is None or isinstance(value, bool):
            raise SchemaError("Identity must be a non-null canonical integer, string, or UUID.")
        result = str(value)
        if not self.is_canonical(result, using=using):
            raise SchemaError(f"Non-canonical or out-of-range resource identity: {result!r}")
        return result

    def to_wire(self, expr: Expression | str) -> Expression:
        return _Conversion(expr, codec=self, to_wire=True)

    def to_column(self, expr: Expression | str) -> Expression:
        return _Conversion(expr, codec=self, to_wire=False)

    def _valid(self, text: Expression, connection: BaseDatabaseWrapper) -> Q:
        valid = (
            Q(GreaterThan(Length(text), Value(0)))
            & Q(LessThanOrEqual(Length(text), Value(64)))
            & ~Q(Exact(text, Value("*")))
        )
        if self.kind == "integer":
            minimum, maximum = connection.ops.integer_field_range(self.field.get_internal_type())
            assert minimum is not None and maximum is not None
            # Regex alone isn't enough: '$' can match before a final newline.
            valid &= Q(Regex(text, _INTEGER)) & ~Q(Regex(text, r"[^0-9-]"))
            positive = ~Q(Regex(text, "^-")) & _digits_at_most(text, maximum)
            negative = (
                Q(Regex(text, "^-")) & _digits_at_most(Substr(text, 2), -minimum)
                if minimum < 0
                else Q(Exact(Value(1), Value(0)))
            )
            valid &= positive | negative
        elif self.kind == "uuid":
            valid &= Q(Regex(text, _UUID)) & ~Q(Regex(text, r"[^0-9a-f-]"))
        else:
            valid &= Q(LessThanOrEqual(Length(text), Value(self.max_length)))
        return valid


_CONVERTED: Any = object()
# (alias, vendor, native uuid, codec, direction) -> the guard's SQL split at the
# converted expression's positions, and its parameters with _CONVERTED markers.
_fragments: dict[tuple[Any, ...], tuple[tuple[str, ...], tuple[Any, ...]]] = {}


class _Converted(Expression):
    """Stands for the converted expression while its guard is compiled."""

    def __init__(self) -> None:
        super().__init__(output_field=models.TextField())

    def as_sql(self, compiler: SQLCompiler, connection: BaseDatabaseWrapper) -> tuple[str, Any]:
        return "%s", (_CONVERTED,)


def _chunks(sql: str, params: Sequence[Any]) -> tuple[str, ...]:
    """Split compiled SQL at the placeholders that stand for the converted expression."""
    chunks: list[str] = []
    start = position = index = 0
    while (position := sql.find("%", position)) != -1:
        if sql.startswith("%%", position):
            position += 2
            continue
        assert sql.startswith("%s", position), "Django compiles positional placeholders"
        if params[index] is _CONVERTED:
            chunks.append(sql[start:position])
            start = position + 2
        index += 1
        position += 2
    chunks.append(sql[start:])
    return tuple(chunks)


class _Conversion(Func):
    """Select vendor-native ORM expressions at compilation, without SQL text."""

    def __init__(self, expr: Expression | str, *, codec: _IdentityCodec, to_wire: bool) -> None:
        self.codec = codec
        self.to_wire = to_wire
        super().__init__(
            F(expr) if isinstance(expr, str) else expr,
            output_field=models.TextField() if to_wire else codec.field,
        )

    def as_sql(
        self,
        compiler: SQLCompiler,
        connection: BaseDatabaseWrapper,
        function: str | None = None,
        template: str | None = None,
        arg_joiner: str | None = None,
        **extra_context: Any,
    ) -> tuple[str, tuple[Any, ...]]:
        """Compile the guard around the converted expression once per field.

        The guard reads the converted expression about ten times and depends
        only on the field, the direction and the connection. Building,
        resolving and compiling it again for every use was most of the time of
        a maintenance pass. Django compiles it once around a placeholder; each
        use compiles its own expression once and takes the placeholder's
        positions. No SQL is written by hand, and ``_compile_fresh`` is the
        same statement without the cache (pinned by test_index_codec_cache).
        """
        key = (
            connection.alias,
            connection.vendor,
            connection.features.has_native_uuid_field,
            self.codec,
            self.to_wire,
        )
        fragment = _fragments.get(key)
        if fragment is None:
            guard = self._expression(_Converted(), connection).resolve_expression(compiler.query)
            guard_sql, guard_params = compiler.compile(guard)
            fragment = _fragments[key] = _chunks(guard_sql, guard_params), tuple(guard_params)
        chunks, guard_params = fragment
        inner_sql, inner_params = compiler.compile(self.get_source_expressions()[0])
        params: list[Any] = []
        for param in guard_params:
            if param is _CONVERTED:
                params.extend(inner_params)
            else:
                params.append(param)
        return inner_sql.join(chunks), tuple(params)

    def _compile_fresh(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper, **extra_context: Any
    ) -> tuple[str, tuple[Any, ...]]:
        original = self.get_source_expressions()[0]
        expression = self._expression(original, connection).resolve_expression(compiler.query)
        sql, params = compiler.compile(expression)
        return sql, tuple(params)

    def _expression(self, original: Any, connection: BaseDatabaseWrapper) -> Expression:
        text = Cast(original, models.TextField())
        candidate: Expression = text
        if (
            self.to_wire
            and self.codec.kind == "uuid"
            and not connection.features.has_native_uuid_field
        ):
            # UUID columns are char(32) on SQLite/MySQL; the wire is always
            # lower-case, hyphenated UUID text, matching str(UUID(...)).
            raw = Lower(text)
            candidate = Concat(
                Substr(raw, 1, 8),
                Value("-"),
                Substr(raw, 9, 4),
                Value("-"),
                Substr(raw, 13, 4),
                Value("-"),
                Substr(raw, 17, 4),
                Value("-"),
                Substr(raw, 21, 12),
                output_field=models.TextField(),
            )
        condition = self.codec._valid(candidate, connection)
        guarded = Case(
            When(condition, then=candidate), default=Value(None), output_field=models.TextField()
        )
        expression: Expression = guarded
        if not self.to_wire:
            cast_input: Expression = guarded
            if self.codec.kind == "uuid" and not connection.features.has_native_uuid_field:
                cast_input = Replace(guarded, Value("-"), Value(""))
            # Guard the cast INPUT, including literal expressions, so no
            # optimizer can eagerly cast an invalid constant in a CASE arm.
            expression = Cast(cast_input, self.codec.field)
        return expression


def identity_codec(model: type[models.Model], attr: str | None = None) -> Codec:
    attr = attr or resource_id_attr(model)
    try:
        _, field = model_identity_fields(model, attr)
    except (FieldDoesNotExist, ValueError) as exc:
        raise SchemaError(
            f"rebac.E014: unsupported identity {model._meta.label}.{attr}: {exc}"
        ) from exc
    # A custom Python conversion cannot be reproduced by a SQL cast. Accept
    # metadata-only subclasses, but refuse custom identity transformations.
    conversions = (
        "to_python",
        "from_db_value",
        "get_prep_value",
        "get_db_prep_value",
        "value_to_string",
    )
    for cls in type(field).__mro__:
        if cls.__module__.startswith("django.db.models."):
            break
        if any(name in cls.__dict__ for name in conversions):
            raise SchemaError(
                f"rebac.E014: unsupported encoded identity {model._meta.label}.{attr}; "
                "custom field conversions cannot be reproduced in permission-index SQL."
            )
    if isinstance(field, models.IntegerField):
        return _IdentityCodec(field, "integer")
    if isinstance(field, models.UUIDField):
        return _IdentityCodec(field, "uuid")
    if isinstance(field, (models.CharField, models.TextField)):
        return _IdentityCodec(field, "text")
    raise SchemaError(
        f"rebac.E014: unsupported identity {model._meta.label}.{attr} "
        f"({field.get_internal_type()}); use integer/auto, char/text/slug, or UUID."
    )
