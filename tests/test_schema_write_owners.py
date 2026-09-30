"""Policy writes and their revision witness have one Django transaction owner."""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from rebac.models import (
    SchemaCaveat,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
)
from rebac.models.generation import SchemaGeneration

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True, params=["denormalized", "registry"])
def storage(request, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param


def policy_row(kind, using="default"):
    definition = SchemaDefinition.objects.using(using).create(resource_type="test/object")
    if kind == "definition":
        return SchemaDefinition(resource_type="test/extra"), {"resource_type": "test/renamed"}
    if kind == "relation":
        return SchemaRelation(definition=definition, name="extra"), {"name": "renamed"}
    if kind == "permission":
        return SchemaPermission(definition=definition, name="extra", expression="nil"), {
            "expression": "authenticated"
        }
    if kind == "caveat":
        return SchemaCaveat(name="extra", expression="true"), {"expression": "false"}
    ct = ContentType.objects.db_manager(using).get_for_model(SchemaDefinition)
    return SchemaOverride(
        kind="disable",
        target_ct=ct,
        target_pk=definition.pk,
        reason="owner test",
        created_at=timezone.now(),
    ), {"reason": "updated"}


PATHS = [
    "save",
    "save_base",
    "create",
    "get_or_create",
    "update_or_create",
    "bulk_create",
    "instance_update",
    "queryset_update",
    "bulk_update",
    "instance_delete",
    "queryset_delete",
]


@pytest.mark.parametrize("kind", ["definition", "relation", "permission", "caveat", "override"])
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("failure", ["rollback", "publisher"])
def test_every_policy_write_and_revision_are_atomic(kind, path, failure, monkeypatch):
    row, changes = policy_row(kind)
    model = type(row)
    if path in {
        "instance_update",
        "queryset_update",
        "bulk_update",
        "instance_delete",
        "queryset_delete",
    }:
        row.save()
    before = list(model.objects.order_by("pk").values())
    revision = SchemaGeneration.objects.get(pk=1).revision
    published = []
    advance = SchemaGeneration.objects.advance

    def publish(*, using):
        assert connection.in_atomic_block
        advance(using=using)
        published.append(SchemaGeneration.objects.get(pk=1).revision)
        if failure == "publisher":
            raise RuntimeError("publication failed")

    monkeypatch.setattr(SchemaGeneration.objects, "advance", publish)

    def write():
        fields = {
            field.attname: getattr(row, field.attname)
            for field in model._meta.concrete_fields
            if not field.primary_key
        }
        if path == "save":
            row.save()
        elif path == "save_base":
            row.save_base(raw=True)
        elif path == "create":
            model.objects.create(**fields)
        elif path == "get_or_create":
            model.objects.get_or_create(**fields)
        elif path == "update_or_create":
            model.objects.update_or_create(**fields)
        elif path == "bulk_create":
            model.objects.bulk_create([row])
        elif path == "instance_update":
            for name, value in changes.items():
                setattr(row, name, value)
            row.save(update_fields=list(changes))
        elif path == "queryset_update":
            model.objects.filter(pk=row.pk).update(**changes)
        elif path == "bulk_update":
            for name, value in changes.items():
                setattr(row, name, value)
            model.objects.bulk_update([row], list(changes))
        elif path == "instance_delete":
            row.delete()
        else:
            model.objects.filter(pk=row.pk).delete()

    if failure == "publisher":
        with pytest.raises(RuntimeError, match="publication failed"):
            write()
    else:
        with transaction.atomic():
            write()
            assert SchemaGeneration.objects.get(pk=1).revision != revision
            transaction.set_rollback(True)
    assert published and all(value != revision for value in published)
    assert SchemaGeneration.objects.get(pk=1).revision == revision
    assert list(model.objects.order_by("pk").values()) == before


@pytest.mark.parametrize("kind", ["definition", "relation", "permission", "caveat", "override"])
@pytest.mark.parametrize("option", ["ignore_conflicts", "update_conflicts"])
def test_conflict_bulk_paths_are_refused_without_writes(kind, option):
    row, _ = policy_row(kind)
    revision = SchemaGeneration.objects.get(pk=1).revision
    with pytest.raises(ValueError, match="conflict handling"):
        type(row).objects.bulk_create([row], **{option: True})
    assert row.pk is None
    assert SchemaGeneration.objects.get(pk=1).revision == revision


@pytest.mark.parametrize("child", ["relations", "permissions"])
@pytest.mark.parametrize("operation", ["add", "set"])
def test_reverse_relation_bulk_writes_advance_and_rollback(child, operation):
    row, _ = policy_row("relation" if child == "relations" else "permission")
    row.save()
    original_parent = row.definition_id
    destination = SchemaDefinition.objects.create(resource_type="test/destination")
    revision = SchemaGeneration.objects.get(pk=1).revision
    with transaction.atomic():
        manager = getattr(destination, child)
        if operation == "add":
            manager.add(row, bulk=True)
        else:
            manager.set([row], bulk=True)
        assert SchemaGeneration.objects.get(pk=1).revision != revision
        row.refresh_from_db()
        assert row.definition_id == destination.pk
        transaction.set_rollback(True)
    row.refresh_from_db()
    assert row.definition_id == original_parent
    assert SchemaGeneration.objects.get(pk=1).revision == revision


@pytest.mark.parametrize("origin", ["definition", "content_type", "user"])
def test_cascade_and_set_null_writes_advance_and_rollback(origin):
    permission, _ = policy_row("permission")
    permission.save()
    ct = ContentType.objects.create(app_label="owner_test", model="cascade")
    user = get_user_model().objects.create(username="schema-author")
    override = SchemaOverride.objects.create(
        kind="disable", target_ct=ct, target_pk=permission.pk, created_by=user
    )
    parent = {"definition": permission.definition, "content_type": ct, "user": user}[origin]
    revision = SchemaGeneration.objects.get(pk=1).revision
    with transaction.atomic():
        parent.delete()
        assert SchemaGeneration.objects.get(pk=1).revision != revision
        if origin == "definition":
            assert not SchemaPermission.objects.filter(pk=permission.pk).exists()
        elif origin == "content_type":
            assert not SchemaOverride.objects.filter(pk=override.pk).exists()
        else:
            override.refresh_from_db()
            assert override.created_by_id is None
        transaction.set_rollback(True)
    assert SchemaGeneration.objects.get(pk=1).revision == revision
    assert SchemaPermission.objects.filter(pk=permission.pk).exists()
    override.refresh_from_db()
    assert override.created_by_id is not None


def installed_database_objects(db):
    """Upgrade assertion only: production has no database-object probes."""
    with db.cursor() as cursor:
        if db.vendor == "sqlite":
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'rebac_%'"
            )
        elif db.vendor == "postgresql":
            cursor.execute(
                "SELECT tgname FROM pg_trigger WHERE tgname LIKE 'rebac_%' "
                "UNION ALL SELECT proname FROM pg_proc WHERE proname='rebac_schema_changed'"
            )
        else:
            cursor.execute(
                "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME LIKE 'rebac_%' "
                "UNION ALL SELECT ROUTINE_NAME FROM information_schema.ROUTINES "
                "WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME='rebac_schema_changed'"
            )
        return {row[0] for row in cursor.fetchall()}


def upgrade_schema_owners(db):
    """Exercise actual 0.21.0 -> 0.22.0 migration states on each vendor."""
    previous = [("rebac", "0005_schema_generation")]
    current = [("rebac", "0007_permission_index")]
    # Start below 0005, since reversing removal deliberately never reinstalls.
    MigrationExecutor(db).migrate([("rebac", "0004_field_backing_path")])
    try:
        executor = MigrationExecutor(db)
        executor.migrate(previous)
        from tests.fixtures.legacy_schema_triggers import install

        historical = executor.loader.project_state(previous).apps
        with db.schema_editor() as editor:
            install(historical, editor)
        assert len(installed_database_objects(db)) == (16 if db.vendor == "postgresql" else 15)
        revision = (
            historical.get_model("rebac", "SchemaGeneration")
            .objects.using(db.alias)
            .get(pk=1)
            .revision
        )
        MigrationExecutor(db).migrate(current)
        assert installed_database_objects(db) == set()
        assert SchemaGeneration.objects.using(db.alias).get(pk=1).revision == revision
        SchemaDefinition.objects.using(db.alias).create(resource_type="upgrade/object")
        assert SchemaGeneration.objects.using(db.alias).get(pk=1).revision != revision
    finally:
        MigrationExecutor(db).migrate(current)


def test_upgrade_removes_installed_database_objects_and_preserves_revision():
    upgrade_schema_owners(connection)


def test_sync_failure_rolls_back_all_policy_rows_and_revision(monkeypatch):
    from rebac.management.commands.rebac import Command

    SchemaGeneration.objects.advance(using="default")
    before = SchemaGeneration.objects.get(pk=1).revision
    prune = Command._prune_package_records

    def fail_after_writes(self, **kwargs):
        prune(self, **kwargs)
        assert SchemaDefinition.objects.exists()
        assert SchemaGeneration.objects.get(pk=1).revision != before
        raise RuntimeError("sync aborted")

    monkeypatch.setattr(Command, "_prune_package_records", fail_after_writes)
    with pytest.raises(RuntimeError, match="sync aborted"):
        call_command("rebac", "sync", stdout=StringIO())
    assert not SchemaDefinition.objects.exists()
    assert SchemaGeneration.objects.get(pk=1).revision == before


def test_bulk_update_late_batch_failure_rolls_back_earlier_batches(monkeypatch):
    definition = SchemaDefinition.objects.create(resource_type="batch/object")
    rows = SchemaPermission.objects.bulk_create(
        [
            SchemaPermission(definition=definition, name=name, expression="nil")
            for name in ("read", "write")
        ]
    )
    before = SchemaGeneration.objects.get(pk=1).revision
    advance = SchemaGeneration.objects.advance
    calls = []

    def fail_second_batch(*, using):
        advance(using=using)
        calls.append(using)
        if len(calls) == 2:
            raise RuntimeError("second batch failed")

    monkeypatch.setattr(SchemaGeneration.objects, "advance", fail_second_batch)
    for row in rows:
        row.expression = "authenticated"
    with pytest.raises(RuntimeError, match="second batch failed"):
        SchemaPermission.objects.bulk_update(rows, ["expression"], batch_size=1)
    assert len(calls) == 2
    assert set(SchemaPermission.objects.values_list("expression", flat=True)) == {"nil"}
    assert SchemaGeneration.objects.get(pk=1).revision == before
