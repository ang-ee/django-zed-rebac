"""Transactional Django write owners for the five policy tables."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any

from django.db import models, router
from django.db.models.base import ModelBase

from .generation import SchemaGeneration, schema_write_atomic


class SchemaQuerySet[T: models.Model](models.QuerySet[T]):
    def update(self, **kwargs: Any) -> int:
        self._for_write = True
        with schema_write_atomic(self.db):
            count = super().update(**kwargs)
            if count:
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
        with schema_write_atomic(self.db):
            rows = super().bulk_create(
                objs, batch_size, ignore_conflicts, update_conflicts, update_fields, unique_fields
            )
            if rows:
                SchemaGeneration.objects.advance(using=self.db)
            return rows

    # Django's bulk_update dispatches every batch through this queryset's update,
    # inside one atomic block. Keep that path as the single owner of batch writes.

    def delete(self) -> tuple[int, dict[str, int]]:
        self._for_write = True
        with schema_write_atomic(self.db):
            result = super().delete()
            if result[0]:
                SchemaGeneration.objects.advance(using=self.db)
            return result


class SchemaRow(models.Model):
    objects = SchemaQuerySet.as_manager()

    class Meta:
        abstract = True
        base_manager_name = "objects"

    def save_base(
        self,
        raw: bool = False,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        alias = using or router.db_for_write(type(self), instance=self)
        with schema_write_atomic(alias):
            super().save_base(raw, force_insert, force_update, alias, update_fields)
            SchemaGeneration.objects.advance(using=alias)

    def delete(
        self, using: str | None = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        alias = using or router.db_for_write(type(self), instance=self)
        with schema_write_atomic(alias):
            result = super().delete(using=alias, keep_parents=keep_parents)
            if result[0]:
                SchemaGeneration.objects.advance(using=alias)
            return result
