"""Transactional Django write owners for the five policy tables."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from django.db import models, router
from django.db.models.base import ModelBase

from .generation import SchemaGeneration, schema_write_atomic

if TYPE_CHECKING:
    from ..index.maintain import IndexMaintenance
    from .overrides import SchemaOverride


@contextmanager
def schema_index_write(using: str) -> Iterator[IndexMaintenance]:
    """Acquire the index lock before any policy read or write."""
    from ..index.maintain import IndexMaintenance

    with IndexMaintenance(using=using) as maintenance, schema_write_atomic(using):
        yield maintenance


def _affected_types(rows: models.QuerySet[Any]) -> set[str] | None:
    from .overrides import SchemaOverride
    from .schema import SchemaDefinition, SchemaPermission, SchemaRelation

    # Class keys also cover proxy subclasses without relying on model names.
    type_paths = {
        SchemaDefinition: "resource_type",
        SchemaRelation: "definition__resource_type",
        SchemaPermission: "definition__resource_type",
    }
    for model, path in type_paths.items():
        if issubclass(rows.model, model):
            return set(rows.values_list(path, flat=True))
    if issubclass(rows.model, SchemaOverride):
        result: set[str] = set()
        for override in rows.select_related("target_ct"):
            target = override.target_ct.model_class()
            target_path = next(
                (
                    path
                    for model, path in type_paths.items()
                    if target is not None and issubclass(target, model)
                ),
                None,
            )
            if target_path is None or target is None:
                return None  # Recaveat changes can reach any definition.
            result.update(
                target._base_manager.using(rows.db)
                .filter(pk=override.target_pk)
                .values_list(target_path, flat=True)
            )
        return result
    return None


def _publish(maintenance: IndexMaintenance, types: set[str] | None) -> None:
    from ..index.maintain import dependent_types

    # Union the OLD dependency closure before losing a removed definition or
    # arrow. finish() adds the new program's closure before deriving.
    if types is not None and not maintenance.schema_all:
        maintenance.schema_types.update(types)
        program = maintenance.program
        if program is not None:
            maintenance.schema_types.update(dependent_types(program, types))
    else:
        maintenance.schema_all = True
        maintenance.schema_types.clear()
    maintenance.changed(schema=True)


def _old_program(maintenance: IndexMaintenance) -> None:
    if maintenance.schema_initial:
        return
    generation = SchemaGeneration.objects.revision_pair(maintenance.using)
    if generation is not None:
        if generation[1] != generation[0] and not maintenance.schema_changed:
            maintenance.schema_all = True
            maintenance.schema_types.clear()
        maintenance.load_program()
    else:
        # Keep the empty pre-sync state until the enclosing owner finishes.
        maintenance.schema_initial = True


def _validate_override_rows(using: str, rows: Iterable[SchemaOverride]) -> None:
    """Reject invalid new arms while allowing old stale arms to be ignored on reads."""
    from ..backends.local import LocalBackend
    from ..composition import compose

    baseline, _deadline = LocalBackend()._load_schema_from_db(using, overrides=())
    compose(baseline, rows)


class SchemaQuerySet[T: models.Model](models.QuerySet[T]):
    def update(self, **kwargs: Any) -> int:
        self._for_write = True
        with schema_index_write(self.db) as maintenance:
            _old_program(maintenance)
            # Policy metadata is schema-sized. Pin PKs before UPDATE changes
            # the caller's predicate (e.g. renaming a definition).
            pks = tuple(self.order_by().values_list("pk", flat=True))
            affected = _affected_types(self)
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
                _publish(maintenance, affected)
                _publish(
                    maintenance,
                    _affected_types(self.model._base_manager.using(self.db).filter(pk__in=pks)),
                )
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
        with schema_index_write(self.db) as maintenance:
            _old_program(maintenance)
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
                _publish(
                    maintenance,
                    _affected_types(
                        self.model._base_manager.using(self.db).filter(
                            pk__in=[row.pk for row in rows]
                        )
                    ),
                )
            return rows

    def bulk_update(
        self, objs: Iterable[T], fields: Iterable[str], batch_size: int | None = None
    ) -> int:
        self._for_write = True
        # update() captures and publishes each batch; the outer pass derives
        # once, after every batch's source statement has completed.
        with schema_index_write(self.db):
            return super().bulk_update(objs, fields, batch_size=batch_size)

    def delete(self) -> tuple[int, dict[str, int]]:
        self._for_write = True
        with schema_index_write(self.db) as maintenance:
            _old_program(maintenance)
            affected = _affected_types(self)
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
                _publish(maintenance, affected)
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
        with schema_index_write(alias) as maintenance:
            _old_program(maintenance)
            affected = _affected_types(type(self)._base_manager.using(alias).filter(pk=self.pk))
            adding = self._state.adding
            super().save_base(raw, force_insert, force_update, alias, update_fields)
            if not raw:
                from .overrides import SchemaOverride

                if isinstance(self, SchemaOverride):
                    _validate_override_rows(alias, [self])
                self._write_effects(created=adding)
            SchemaGeneration.objects.advance(using=alias)
            _publish(maintenance, affected)
            _publish(
                maintenance,
                _affected_types(type(self)._base_manager.using(alias).filter(pk=self.pk)),
            )

    def delete(
        self, using: str | None = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        alias = using or router.db_for_write(type(self), instance=self)
        with schema_index_write(alias) as maintenance:
            _old_program(maintenance)
            affected = _affected_types(type(self)._base_manager.using(alias).filter(pk=self.pk))
            result = super().delete(using=alias, keep_parents=keep_parents)
            if result[0]:
                self._write_effects(deleted=True)
                SchemaGeneration.objects.advance(using=alias)
                _publish(maintenance, affected)
            return result
