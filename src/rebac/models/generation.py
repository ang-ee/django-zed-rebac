"""The revision of the stored policy: its cache witness and its write lock."""

from typing import ClassVar
from uuid import uuid4

from django.db import connections, models, transaction
from django.db.transaction import Atomic


def schema_write_atomic(using: str) -> Atomic:
    # SQLite's manually managed transactions begin at the first write. An
    # earlier SAVEPOINT/RELEASE could commit that write independently. Retain
    # normal nested savepoints when Django owns the outer transaction.
    connection = connections[using]
    return transaction.atomic(
        using=using, savepoint=connection.in_atomic_block and connection.commit_on_exit
    )


class SchemaGenerationManager(models.Manager["SchemaGeneration"]):
    def revision(self, using: str) -> str | None:
        """The published policy revision; ``None`` before the first policy write."""
        found = self.using(using).filter(pk=1).values_list("revision", flat=True).first()
        return found or None

    def lock(self, using: str) -> None:
        """Serialize policy writers on the one generation row, creating it if absent.

        The caller is inside a transaction.  A row created here has an empty
        revision, which reads treat as no published policy.
        """
        rows = self.using(using)
        if connections[using].features.has_select_for_update:
            rows = rows.select_for_update()
        if rows.filter(pk=1).first() is None:
            self.using(using).get_or_create(pk=1, defaults={"revision": ""})
            rows.filter(pk=1).first()

    def advance(self, *, using: str) -> None:
        """Publish policy writes atomically, using a fresh, rollback-safe identity."""
        from ..signals import _mark_schema_caches_stale

        with schema_write_atomic(using):
            rows = self.using(using)
            revision = uuid4().hex
            if not rows.filter(pk=1).update(revision=revision):
                _, created = rows.get_or_create(pk=1, defaults={"revision": revision})
                if not created:
                    # Another writer repaired the row between UPDATE and INSERT.
                    rows.filter(pk=1).update(revision=revision)
            _mark_schema_caches_stale()


class SchemaGeneration(models.Model):
    objects: ClassVar[SchemaGenerationManager] = SchemaGenerationManager()  # pyright: ignore[reportIncompatibleVariableOverride]
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    revision = models.CharField(max_length=32, editable=False)

    class Meta:
        app_label = "rebac"
        default_permissions = ()
