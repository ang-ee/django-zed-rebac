"""Transactional Django write owners for the five policy tables."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

from django.db import models, router
from django.db.models.base import ModelBase

from ..errors import SchemaError
from .generation import SchemaGeneration, schema_write_atomic

if TYPE_CHECKING:
    from .overrides import SchemaOverride

_owners: ContextVar[frozenset[str]] = ContextVar("rebac_schema_owners", default=frozenset())


def _stored_policy_errors(using: str) -> list[str] | None:
    """What refuses the stored policy; ``None`` when it cannot be read as one.

    Only the policy's own rules are checked: its structure and the shape of
    its recursion.  Whether its backings resolve against the current models
    is a system check (``rebac.E009``), not a condition of writing a row, so a
    stored policy can be repaired while it disagrees with the models.
    """
    from ..backends.local import LocalBackend
    from ..compile.program import CompileProgram
    from ..composition import compose_tagged, split_stale_overrides

    try:
        loaded = LocalBackend()._load_schema_from_db(using)
    except SchemaError:
        return None
    baseline = getattr(loaded, "baseline", loaded[0])
    overrides, _stale = split_stale_overrides(baseline, list(getattr(loaded, "overrides", ())))
    try:
        CompileProgram.build(compose_tagged(baseline, overrides).schema)
    except SchemaError as exc:
        return [str(exc)]
    return []


@contextmanager
def schema_index_write(using: str) -> Iterator[None]:
    """One transaction that owns policy writes on ``using``.

    Policy writers serialize on the generation row.  When the outermost owner
    exits, the composed policy is validated under that lock, so two changes
    that are each valid cannot combine into a refused one.  A policy that was
    already refused when the owner started is being repaired and is not
    validated again.
    """
    if using in _owners.get():
        with schema_write_atomic(using):
            yield
        return
    with schema_write_atomic(using):
        SchemaGeneration.objects.lock(using)
        before = SchemaGeneration.objects.revision(using)
        sound = _stored_policy_errors(using) == []
        token = _owners.set(_owners.get() | {using})
        try:
            yield
        finally:
            _owners.reset(token)
        if sound and SchemaGeneration.objects.revision(using) != before:
            errors = _stored_policy_errors(using)
            if errors is None:
                # Raise the reader's own account of what is wrong.
                from ..backends.local import LocalBackend

                LocalBackend()._load_schema_from_db(using)
            elif errors:
                raise SchemaError("; ".join(errors))


@contextmanager
def schema_changes(using: str | None = None) -> Iterator[None]:
    """Group stored-schema writes into one transaction and one validation.

    The block is one transaction on ``using`` (the relationship write alias by
    default) and holds the policy lock until it exits; an exception rolls the
    writes back.  The composed policy is validated once, when the block exits.
    Blocks nest: an inner one joins the outer one.
    """
    from . import active_relationship_model

    alias = using or router.db_for_write(active_relationship_model())
    with schema_index_write(alias):
        yield


def _validate_override_rows(using: str, rows: Iterable[SchemaOverride]) -> None:
    """Reject invalid new arms while allowing old stale arms to be ignored on reads."""
    from ..backends.local import LocalBackend
    from ..composition import compose

    baseline, _deadline = LocalBackend()._load_schema_from_db(using, overrides=())
    compose(baseline, rows)


class SchemaQuerySet[T: models.Model](models.QuerySet[T]):
    def update(self, **kwargs: Any) -> int:
        self._for_write = True
        with schema_index_write(self.db):
            # Policy metadata is schema-sized. Pin PKs before UPDATE changes
            # the caller's predicate (e.g. renaming a definition).
            pks = tuple(self.order_by().values_list("pk", flat=True))
            count = super().update(**kwargs)
            if count:
                from .overrides import SchemaOverride

                if issubclass(self.model, SchemaOverride):
                    _validate_override_rows(
                        self.db,
                        self.model._base_manager.using(self.db).filter(pk__in=pks),
                    )
                    from ..backends import reset_backend

                    reset_backend()
                SchemaGeneration.objects.advance(using=self.db)
            return count

    def bulk_create(
        self,
        objs: Iterable[T],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[T]:
        if ignore_conflicts or update_conflicts:
            raise ValueError("Schema bulk_create does not support conflict handling.")
        self._for_write = True
        with schema_index_write(self.db):
            rows = super().bulk_create(
                objs, batch_size, ignore_conflicts, update_conflicts, update_fields, unique_fields
            )
            if rows:
                from .overrides import SchemaOverride

                if issubclass(self.model, SchemaOverride):
                    _validate_override_rows(self.db, cast("list[SchemaOverride]", rows))
                for row in rows:
                    cast("SchemaRow", row)._write_effects(created=True)
                SchemaGeneration.objects.advance(using=self.db)
            return rows

    def bulk_update(
        self, objs: Iterable[T], fields: Iterable[str], batch_size: int | None = None
    ) -> int:
        self._for_write = True
        # update() publishes each batch; the outer owner validates once.
        with schema_index_write(self.db):
            return super().bulk_update(objs, fields, batch_size=batch_size)

    def delete(self) -> tuple[int, dict[str, int]]:
        self._for_write = True
        with schema_index_write(self.db):
            from .overrides import SchemaOverride

            audit_rows = []
            if issubclass(self.model, SchemaOverride):
                for values in self.values(
                    "kind",
                    "expression",
                    "reason",
                    "target_pk",
                    "target_ct__app_label",
                    "target_ct__model",
                ):
                    target = f"{values['kind']}:{values.pop('target_ct__app_label')}.{values.pop('target_ct__model')}/{values['target_pk']}"
                    row = SchemaOverride(**values)
                    row._audit_target = target
                    audit_rows.append(row)
            result = super().delete()
            for row in audit_rows:
                row._write_effects(deleted=True)
            if result[0]:
                SchemaGeneration.objects.advance(using=self.db)
            return result


class SchemaRow(models.Model):
    objects = SchemaQuerySet.as_manager()

    class Meta:
        abstract = True
        base_manager_name = "objects"

    def _write_effects(self, *, created: bool = False, deleted: bool = False) -> None:
        """Model-owned side effects; overrides add backend reset and audit."""

    def save_base(
        self,
        raw: bool = False,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        alias = using or router.db_for_write(type(self), instance=self)
        with schema_index_write(alias):
            adding = self._state.adding
            super().save_base(raw, force_insert, force_update, alias, update_fields)
            if not raw:
                from .overrides import SchemaOverride

                if isinstance(self, SchemaOverride):
                    _validate_override_rows(alias, [self])
                self._write_effects(created=adding)
            SchemaGeneration.objects.advance(using=alias)

    def delete(
        self, using: str | None = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        alias = using or router.db_for_write(type(self), instance=self)
        with schema_index_write(alias):
            result = super().delete(using=alias, keep_parents=keep_parents)
            if result[0]:
                self._write_effects(deleted=True)
                SchemaGeneration.objects.advance(using=alias)
            return result
