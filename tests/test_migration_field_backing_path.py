"""Migration 0004 renames the persisted field-backing key without touching provenance."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.utils import timezone

from rebac.management.commands.rebac import Command
from rebac.models import PackageManagedRecord, SchemaDefinition, SchemaRelation

migration = importlib.import_module("rebac.migrations.0004_field_backing_path")
_schema_editor = SimpleNamespace(connection=connection)


def _relation(name: str, backing: dict | None, definition: SchemaDefinition) -> SchemaRelation:
    return SchemaRelation.objects.create(
        definition=definition,
        name=name,
        allowed_subjects=[{"type": "blog/folder"}],
        backing=backing,
    )


@pytest.mark.django_db
def test_forward_renames_only_field_backings_and_preserves_other_keys() -> None:
    post_def = SchemaDefinition.objects.create(resource_type="blog/post")
    explicit = _relation("folder", {"kind": "fk", "attname": "folder"}, post_def)
    implicit_kind = _relation("parent", {"attname": "parent"}, post_def)
    filtered = _relation(
        "editor",
        {"kind": "fk", "attname": "roster__user", "filters": {"roster__active": True}},
        post_def,
    )
    const = _relation("admin", {"kind": "const", "target_id": "admin"}, post_def)
    attribute = _relation("kind", {"kind": "attribute", "field": "kind"}, post_def)
    stored = _relation("viewer", None, post_def)

    migration.forwards(apps, _schema_editor)

    for row in (explicit, implicit_kind, filtered, const, attribute, stored):
        row.refresh_from_db()
    assert explicit.backing == {"kind": "fk", "path": "folder"}
    assert implicit_kind.backing == {"path": "parent"}
    assert filtered.backing == {
        "kind": "fk",
        "path": "roster__user",
        "filters": {"roster__active": True},
    }
    assert const.backing == {"kind": "const", "target_id": "admin"}
    assert attribute.backing == {"kind": "attribute", "field": "kind"}
    assert stored.backing is None


@pytest.mark.django_db
def test_backward_is_the_symmetric_rename() -> None:
    post_def = SchemaDefinition.objects.create(resource_type="blog/post")
    row = _relation("folder", {"kind": "fk", "path": "folder"}, post_def)

    migration.backwards(apps, _schema_editor)
    row.refresh_from_db()
    assert row.backing == {"kind": "fk", "attname": "folder"}

    migration.forwards(apps, _schema_editor)
    row.refresh_from_db()
    assert row.backing == {"kind": "fk", "path": "folder"}


@pytest.mark.django_db
def test_conflicting_keys_are_rejected_instead_of_overwritten() -> None:
    post_def = SchemaDefinition.objects.create(resource_type="blog/post")
    _relation("folder", {"kind": "fk", "attname": "folder", "path": "other"}, post_def)

    with pytest.raises(ValueError, match="both 'attname' and 'path'"):
        migration.forwards(apps, _schema_editor)


def _managed(row: SchemaRelation, payload: dict, *, external_id: str) -> PackageManagedRecord:
    natural_key = {"definition": row.definition, "name": row.name}
    return PackageManagedRecord.objects.create(
        package="testapp",
        external_id=external_id,
        schema_revision=1,
        target_ct=ContentType.objects.get_for_model(SchemaRelation),
        target_pk=row.pk,
        content_hash=Command._hash_payload({**natural_key, **payload}),
        no_update=True,
        last_synced_at=timezone.now(),
    )


@pytest.mark.django_db
def test_migrated_rows_sync_without_drift_while_admin_edits_stay_drift() -> None:
    post_def = SchemaDefinition.objects.create(resource_type="blog/post")
    old_payload = {
        "allowed_subjects": [{"type": "blog/folder"}],
        "backing": {"kind": "fk", "attname": "folder"},
        "caveat": "",
        "with_expiration": False,
    }
    new_payload = {**old_payload, "backing": {"kind": "fk", "path": "folder"}}
    synced = _relation("folder", old_payload["backing"], post_def)
    _managed(synced, old_payload, external_id="blog/post#folder")
    edited = _relation("parent", old_payload["backing"], post_def)
    edited.allowed_subjects = [{"type": "blog/post"}]  # admin edit
    edited.save(update_fields=["allowed_subjects"])
    _managed(edited, old_payload, external_id="blog/post#parent")

    migration.forwards(apps, _schema_editor)

    command = Command()
    command.stdout = __import__("io").StringIO()
    sync = lambda row, check_only: command._sync_row(  # noqa: E731
        SchemaRelation,
        {"definition": post_def, "name": row.name},
        new_payload,
        "testapp",
        f"blog/post#{row.name}",
        check_only,
        False,
    )
    # Migrated row equals the declared payload: no drift, and an ordinary
    # (non-check) sync refreshes the stale provenance hash in place.
    assert sync(synced, True) is False
    assert sync(synced, False) is False
    record = PackageManagedRecord.objects.get(external_id="blog/post#folder")
    assert record.content_hash == Command._hash_payload(
        {"definition": post_def, "name": "folder", **new_payload}
    )
    # A real admin edit under no_update stays drift after the migration.
    assert sync(edited, True) is True
