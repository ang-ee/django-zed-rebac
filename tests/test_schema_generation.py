"""Persisted revisions bound schema reads without caching live authorization rows."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from importlib import import_module
from io import StringIO
from threading import Barrier, Event

import pytest
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.test.utils import CaptureQueriesContext

from rebac import (
    LocalBackend,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    backend,
    check_new,
    evaluator_scope,
    sudo,
)
from rebac.backends import reset_backend
from rebac.checks import check_schema_generation
from rebac.models import (
    SchemaCaveat,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
)
from rebac.models.generation import SchemaGeneration

from .testapp.models import Folder, Post

pytestmark = pytest.mark.django_db(transaction=True)
ACTOR = SubjectRef.of("auth/user", "reader")


@pytest.fixture(autouse=True)
def fresh_process_trigger_checks(monkeypatch):
    # Each test can simulate process startup against intact or damaged metadata.
    monkeypatch.setattr("rebac.schema.generation._trigger_checks", {})


@pytest.fixture(params=["denormalized", "registry"])
def synced(request, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    call_command("rebac", "sync", stdout=StringIO())
    SchemaDefinition.objects.create(resource_type="auth/user")
    group = SchemaDefinition.objects.create(resource_type="auth/group")
    SchemaRelation.objects.create(
        definition=group, name="member", allowed_subjects=[{"type": "auth/user"}]
    )
    with sudo(reason="schema revision fixture"):
        post = Post.objects.create(title="cached schema")
    reset_backend()
    local = backend()
    resource = ObjectRef("blog/post", str(post.pk))
    local.write_relationships([RelationshipTuple(resource, "owner", ACTOR)])
    yield local, resource
    reset_backend()


def _check(local, resource):
    return local.check_access(subject=ACTOR, action="write", resource=resource).allowed


def _revision():
    return SchemaGeneration.objects.get(pk=1).revision


def _loads(queries):
    return sum('FROM "rebac_schemadefinition"' in query["sql"] for query in queries)


def _generation_reads(queries):
    return sum('FROM "rebac_schemageneration"' in query["sql"] for query in queries)


def _permission():
    return SchemaPermission.objects.get(definition__resource_type="blog/post", name="write")


def _raw_permission(expression, pk, *, using=connection):
    with using.cursor() as cursor:
        cursor.execute(
            'UPDATE "rebac_schemapermission" SET "expression" = %s WHERE "id" = %s',
            [expression, pk],
        )


@pytest.mark.parametrize("atomic", [False, True])
@pytest.mark.parametrize("surface, warm_cost", [("check", 2), ("queryset", 2), ("new", 1)])
def test_schema_query_budget(synced, monkeypatch, atomic, surface, warm_cost):
    _, resource = synced
    local = LocalBackend()
    monkeypatch.setattr("rebac.backends._backend", local)

    def evaluate():
        if surface == "check":
            assert _check(local, resource)
        elif surface == "queryset":
            assert len(list(Post.objects.with_actor(ACTOR).with_action("write"))) == 1
        else:
            assert check_new(
                subject=ACTOR,
                action="write",
                resource_type="blog/post",
                relationships={"owner": [ACTOR]},
                backend=local,
            ).allowed

    with transaction.atomic() if atomic else nullcontext():
        with CaptureQueriesContext(connection) as cold:
            evaluate()
        # Five component reads plus one stability read on the first load.
        assert len(cold) == warm_cost + 6
        assert _loads(cold) == 1
        with CaptureQueriesContext(connection) as warm:
            for _ in range(10):
                evaluate()
        assert len(warm) == warm_cost * 10
        assert _loads(warm) == 0
        assert _generation_reads(warm) == 10


@pytest.mark.parametrize("atomic", [False, True])
@pytest.mark.parametrize("surface", ["check", "queryset", "new", "decision"])
def test_evaluator_scope_query_budget(synced, atomic, surface):
    local, resource = synced
    with transaction.atomic() if atomic else nullcontext(), evaluator_scope() as evaluator:

        def evaluate():
            if surface == "check":
                assert _check(local, resource)
            elif surface == "queryset":
                assert len(list(Post.objects.with_actor(ACTOR).with_action("write"))) == 1
            elif surface == "decision":
                assert evaluator.check(
                    local, subject=ACTOR, action="write", resource=resource
                ).allowed
            else:
                assert check_new(
                    subject=ACTOR,
                    action="write",
                    resource_type="blog/post",
                    relationships={"owner": [ACTOR]},
                    backend=local,
                ).allowed

        with CaptureQueriesContext(connection) as first:
            evaluate()
        assert len(first) == (1 if surface == "new" else 2)
        assert _generation_reads(first) == 1
        for n in (1, 10, 100):
            with CaptureQueriesContext(connection) as repeated:
                for _ in range(n):
                    evaluate()
            expected = 0 if surface == "new" or (surface == "decision" and not atomic) else n
            assert len(repeated) == expected
            assert _generation_reads(repeated) == _loads(repeated) == 0


def test_evaluator_revalidates_once_per_transaction_boundary(synced):
    local, resource = synced
    with evaluator_scope() as evaluator:
        assert evaluator.check(local, subject=ACTOR, action="write", resource=resource).allowed
        for _ in range(2):
            with transaction.atomic():
                with CaptureQueriesContext(connection) as queries:
                    assert _check(local, resource)
                    assert _check(local, resource)
                assert _generation_reads(queries) == 1
            with CaptureQueriesContext(connection) as queries:
                assert evaluator.check(
                    local, subject=ACTOR, action="write", resource=resource
                ).allowed
            assert _generation_reads(queries) == 1


@pytest.mark.parametrize("scoped", [False, True])
def test_absent_witness_never_caches_a_revoked_grant(synced, scoped):
    local, resource = synced
    permission = _permission()
    migration = import_module("rebac.migrations.0005_schema_generation")
    with connection.schema_editor() as editor:
        migration.uninstall(apps, editor)
    SchemaGeneration.objects.all().delete()
    other = connection.copy(alias="unwitnessed_writer")
    try:
        errors = check_schema_generation(databases=["default"])
        assert [error.id for error in errors] == ["rebac.E012"]
        assert "missing table or revision row" in errors[0].msg
        with evaluator_scope() if scoped else nullcontext() as evaluator:

            def check():
                if evaluator is not None:
                    return evaluator.check(
                        local, subject=ACTOR, action="write", resource=resource
                    ).allowed
                return _check(local, resource)

            assert check()
            _raw_permission("nil", permission.pk, using=other)
            assert not check()
            assert not check()
            if evaluator is not None:
                assert evaluator.stats()["check_entries"] == 0
    finally:
        BaseDatabaseWrapper.close(other)
        with connection.schema_editor() as editor:
            SchemaGeneration.objects.all().delete()
            migration.install(apps, editor)


def test_missing_triggers_are_reported_even_with_revision_row(synced):
    migration = import_module("rebac.migrations.0005_schema_generation")
    assert check_schema_generation(databases=["default"]) == []
    with connection.schema_editor() as editor:
        migration.uninstall(apps, editor)
        migration.uninstall(apps, editor)  # Partial reversals are safe to retry.
    try:
        errors = check_schema_generation(databases=["default"])
        assert [error.id for error in errors] == ["rebac.E012"]
        assert "missing table or revision row" not in errors[0].msg
        assert "rebac_schemapermission_update_generation" in errors[0].msg
    finally:
        SchemaGeneration.objects.all().delete()
        with connection.schema_editor() as editor:
            SchemaGeneration.objects.all().delete()
            migration.install(apps, editor)


def test_missing_witness_table_after_migration_is_reported(synced):
    migration = import_module("rebac.migrations.0005_schema_generation")
    with connection.schema_editor() as editor:
        migration.uninstall(apps, editor)
        editor.delete_model(SchemaGeneration)
    try:
        assert [e.id for e in check_schema_generation(databases=["default"])] == ["rebac.E012"]
    finally:
        with connection.schema_editor() as editor:
            editor.create_model(SchemaGeneration)
            SchemaGeneration.objects.all().delete()
            migration.install(apps, editor)


@pytest.mark.parametrize("atomic", [False, True])
def test_unmigrated_database_loads_uncached(synced, atomic):
    local, resource = synced
    permission = _permission()
    try:
        call_command("migrate", "rebac", "0004", verbosity=0)
        # The migration command's preflight must permit installing its witness.
        assert check_schema_generation(databases=["default"]) == []
        with transaction.atomic() if atomic else nullcontext(), evaluator_scope() as evaluator:
            assert evaluator.check(local, subject=ACTOR, action="write", resource=resource).allowed
            _raw_permission("nil", permission.pk)
            assert not evaluator.check(
                local, subject=ACTOR, action="write", resource=resource
            ).allowed
        assert not _check(local, resource)
    finally:
        call_command("migrate", "rebac", "0005", verbosity=0)


def test_revision_reads_can_run_concurrently(synced):
    local, resource = synced
    barrier = Barrier(4)

    def instrument(execute, sql, params, many, context):
        if 'FROM "rebac_schemageneration"' in sql:
            # Deterministic regression for a lock held during network I/O:
            # all four queries must enter before any one can finish.
            barrier.wait(timeout=5)
        return execute(sql, params, many, context)

    def worker():
        db = connections["default"]
        try:
            with db.execute_wrapper(instrument):
                return _check(local, resource)
        finally:
            BaseDatabaseWrapper.close(db)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(worker) for _ in range(4)]
        assert all(future.result() for future in futures)


def test_revision_races_have_a_bounded_retry_budget(synced, monkeypatch):
    _, resource = synced
    local = LocalBackend()
    reads = []

    def changing_revision(connection):
        reads.append(connection.alias)
        return str(len(reads))

    monkeypatch.setattr(local, "_read_schema_revision", changing_revision)
    load = local._load_schema_from_db
    loads = []

    def count_load(using):
        loads.append(using)
        return load(using)

    monkeypatch.setattr(local, "_load_schema_from_db", count_load)
    snapshot = local._schema_snapshot()
    assert snapshot.revision is None
    assert snapshot.generation == -1
    assert len(reads) == 6
    assert len(loads) == 4  # Three attempts, then one uncached fallback.
    assert local._schema_loads == local._schema_snapshots == {}
    with evaluator_scope() as evaluator:
        assert evaluator.check(local, subject=ACTOR, action="write", resource=resource).allowed
        _raw_permission("nil", _permission().pk)
        assert not evaluator.check(local, subject=ACTOR, action="write", resource=resource).allowed
        assert evaluator.stats()["check_entries"] == 0
    assert local._schema_snapshots == local._schema_facts_memo == {}


def test_generation_model_is_private():
    import rebac.models

    assert not hasattr(rebac.models, "SchemaGeneration")
    assert SchemaGeneration._meta.default_permissions == ()


@pytest.mark.parametrize("external", [False, True])
def test_sync_invalidates_existing_backend_on_next_check(synced, monkeypatch, external):
    local, resource = synced
    permission = _permission()
    _raw_permission("viewer", permission.pk)
    assert not _check(local, resource)
    before = _revision()
    invalidation = local._schema_invalidation_generation

    def sync():
        call_command("rebac", "sync", "--force-overwrite", "--yes", stdout=StringIO())

    def worker():
        try:
            sync()
        finally:
            BaseDatabaseWrapper.close(connections["default"])

    if external:
        # The writer has its own DB connection and cannot notify this backend,
        # as when sync is run by a separate process.
        monkeypatch.setattr("rebac.signals._mark_schema_caches_stale", lambda: None)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(worker).result()
        assert local._schema_invalidation_generation == invalidation
    else:
        sync()
    assert _revision() != before
    with CaptureQueriesContext(connection) as changed:
        assert _check(local, resource)
    assert _loads(changed) == 1
    assert _generation_reads(changed) == 2
    with CaptureQueriesContext(connection) as warm:
        assert _check(local, resource)
    assert len(warm) == 2


def test_noop_sync_publishes_revision_for_trigger_free_maintenance(synced):
    local, _ = synced
    before, schema = _revision(), local.schema()
    call_command("rebac", "sync", stdout=StringIO())
    assert _revision() != before
    with CaptureQueriesContext(connection) as queries:
        assert local.schema() == schema
        assert local.schema() is not schema
    assert _loads(queries) == 1


def test_rolled_back_revision_cannot_alias_a_later_schema(synced):
    local, resource = synced
    permission = _permission()
    _raw_permission("viewer", permission.pk)
    original = _revision()
    assert not _check(local, resource)
    with transaction.atomic():
        _raw_permission("owner", permission.pk)
        temporary = _revision()
        assert _check(local, resource)
        transaction.set_rollback(True)
    assert _revision() == original
    # No intervening permission read after rollback: a counter could reuse the
    # temporary revision here and incorrectly grant from its cached AST.
    with transaction.atomic():
        _raw_permission("viewer", permission.pk)
        assert _revision() not in (original, temporary)
        assert not _check(local, resource)
    assert not _check(local, resource)


@pytest.mark.parametrize("boundary", ["atomic", "savepoint", "manual"])
def test_rollback_without_evaluator_discards_temporary_schema(synced, boundary):
    local, resource = synced
    permission = _permission()
    _raw_permission("viewer", permission.pk)
    assert not _check(local, resource)
    if boundary == "manual":
        transaction.set_autocommit(False)
        try:
            _raw_permission("owner", permission.pk)
            assert _check(local, resource)
            transaction.rollback()
            assert not _check(local, resource)
        finally:
            transaction.rollback()
            transaction.set_autocommit(True)
    else:
        with transaction.atomic():
            savepoint = transaction.savepoint() if boundary == "savepoint" else None
            _raw_permission("owner", permission.pk)
            assert _check(local, resource)
            if savepoint is not None:
                transaction.savepoint_rollback(savepoint)
                assert not _check(local, resource)
                transaction.savepoint_commit(savepoint)
            else:
                transaction.set_rollback(True)
        assert not _check(local, resource)


def test_load_retries_when_another_connection_changes_revision(synced, monkeypatch):
    _, resource = synced
    permission = _permission()
    local = LocalBackend()
    load = local._load_schema_from_db
    other = connection.copy(alias="schema_writer")
    calls = []

    def racing_load(using):
        result = load(using)
        calls.append(using)
        if len(calls) == 1:
            _raw_permission("viewer", permission.pk, using=other)
        return result

    monkeypatch.setattr(local, "_load_schema_from_db", racing_load)
    try:
        assert not _check(local, resource)
        assert calls == ["default", "default"]
        assert not _check(local, resource)
        assert len(calls) == 2
    finally:
        BaseDatabaseWrapper.close(other)


def test_shared_backend_threads_parse_and_compute_facts_once(synced, monkeypatch):
    _, resource = synced
    local = LocalBackend()
    barrier = Barrier(8)
    loads = []
    facts = []
    load = local._load_schema_from_db
    from rebac.schema import introspection

    live_types = introspection.live_backed_resource_types

    def count_load(using):
        loads.append(using)
        return load(using)

    def count_facts(schema):
        facts.append(schema)
        return live_types(schema)

    monkeypatch.setattr(local, "_load_schema_from_db", count_load)
    monkeypatch.setattr(introspection, "live_backed_resource_types", count_facts)

    def worker():
        db = connections["default"]
        try:
            barrier.wait(timeout=5)
            assert _check(local, resource)
            with CaptureQueriesContext(db) as repeated:
                for _ in range(4):
                    with evaluator_scope() as evaluator:
                        assert evaluator.check(
                            local, subject=ACTOR, action="write", resource=resource
                        ).allowed
            return _loads(repeated), len(repeated), local.schema()
        finally:
            BaseDatabaseWrapper.close(db)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker) for _ in range(8)]
        results = [future.result() for future in futures]
    parent_schema = local.schema()
    for warm, cost, schema in results:
        assert warm == 0
        assert cost == 8
        assert schema is parent_schema
    assert len(loads) == len(facts) == local._schema_generation == 1


def test_signal_invalidation_does_not_wait_on_database_loader(synced, monkeypatch):
    _, resource = synced
    local = LocalBackend()
    entered, release = Event(), Event()
    load = local._load_schema_from_db
    calls = []

    def slow_load(using):
        schema = load(using)
        calls.append(using)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=5)
        return schema

    def worker():
        try:
            return _check(local, resource)
        finally:
            BaseDatabaseWrapper.close(connections["default"])

    monkeypatch.setattr(local, "_load_schema_from_db", slow_load)
    with ThreadPoolExecutor(max_workers=2) as executor:
        loading = executor.submit(worker)
        assert entered.wait(timeout=5)
        try:
            # A signal holding DB write locks must not wait for the loader's I/O.
            executor.submit(local.mark_schema_stale).result(timeout=2)
        finally:
            release.set()
        assert loading.result(timeout=5)
    assert len(calls) == 2  # Invalidation also rejects the in-flight publication.


@pytest.mark.parametrize("kind", ["definition", "relation", "permission", "caveat", "override"])
def test_generation_tracks_bulk_insert_update_and_delete_on_every_schema_table(synced, kind):
    definition = SchemaDefinition.objects.get(resource_type="blog/post")
    if kind == "definition":
        model = SchemaDefinition
        row = model(resource_type="extra/object")
        changes = {"resource_type": "extra/renamed"}
    elif kind == "relation":
        model = SchemaRelation
        row = model(definition=definition, name="extra", allowed_subjects=[])
        changes = {"name": "renamed"}
    elif kind == "permission":
        model = SchemaPermission
        row = model(definition=definition, name="extra", expression="nil")
        changes = {"expression": "owner"}
    elif kind == "caveat":
        model = SchemaCaveat
        row = model(name="extra", params=[], expression="true")
        changes = {"expression": "false"}
    else:
        model = SchemaOverride
        row = model(
            kind="loosen",
            target_ct=ContentType.objects.get_for_model(SchemaPermission),
            target_pk=_permission().pk,
            expression="owner",
            reason="revision fixture",
        )
        changes = {"expression": "viewer"}
    revisions = [_revision()]
    model.objects.bulk_create([row])
    revisions.append(_revision())
    model.objects.filter(pk=row.pk).update(**changes)
    revisions.append(_revision())
    # Raw delete deliberately bypasses Django's signals and delete collector.
    table = connection.ops.quote_name(model._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {table} WHERE id = %s", [row.pk])
    revisions.append(_revision())
    assert len(set(revisions)) == 4


def test_generation_migration_reverses_without_changing_schema(synced):
    local, resource = synced
    before = list(SchemaPermission.objects.order_by("pk").values())
    revision = _revision()
    try:
        call_command("migrate", "rebac", "0004", verbosity=0)
        assert "rebac_schemageneration" not in connection.introspection.table_names()
        assert list(SchemaPermission.objects.order_by("pk").values()) == before
    finally:
        call_command("migrate", "rebac", "0005", verbosity=0)
    assert list(SchemaPermission.objects.order_by("pk").values()) == before
    assert _revision() != revision
    assert _check(local, resource)
    _raw_permission("viewer", _permission().pk)
    assert not _check(local, resource)


def test_cached_schema_keeps_filtered_constants_and_fields_live(synced):
    local, _ = synced
    audience = SchemaDefinition.objects.create(resource_type="site/audience")
    SchemaPermission.objects.create(definition=audience, name="read", expression="authenticated")
    folder = SchemaDefinition.objects.get(resource_type="blog/folder")
    SchemaRelation.objects.create(
        definition=folder,
        name="public",
        allowed_subjects=[{"type": "site/audience"}],
        backing={"kind": "const", "target_id": "public", "filters": {"is_active": True}},
    )
    SchemaPermission.objects.filter(definition=folder, name="read").update(
        expression="public->read"
    )
    SchemaRelation.objects.filter(definition__resource_type="blog/post", name="folder").update(
        backing={"kind": "fk", "path": "folder"}
    )
    SchemaPermission.objects.filter(definition__resource_type="blog/post", name="read").update(
        expression="folder->read"
    )
    with sudo(reason="live backing fixture"):
        row = Folder.objects.create(name="live", is_active=True)
        post = Post.objects.create(title="live", folder=row)
    resource = ObjectRef("blog/post", str(post.pk))
    with evaluator_scope() as evaluator:
        assert evaluator.check(local, subject=ACTOR, action="read", resource=resource).allowed
        revision = _revision()
        Folder._base_manager.filter(pk=row.pk).update(is_active=False)
        with CaptureQueriesContext(connection) as queries:
            assert not evaluator.check(
                local, subject=ACTOR, action="read", resource=resource
            ).allowed
            assert not Post.objects.with_actor(ACTOR).filter(pk=post.pk).exists()
        assert _loads(queries) == 0
        assert _revision() == revision
        Folder._base_manager.filter(pk=row.pk).update(is_active=True)
        assert evaluator.check(local, subject=ACTOR, action="read", resource=resource).allowed
        Post._base_manager.filter(pk=post.pk).update(folder=None)
        assert not evaluator.check(local, subject=ACTOR, action="read", resource=resource).allowed


def test_schema_and_override_cache_follow_routed_database(synced, tmp_path, django_db_blocker):
    local, resource = synced
    alias = "schema_target"
    target = connection.copy(alias=alias)
    target.settings_dict["NAME"] = str(tmp_path / "schema.sqlite3")
    connections[alias] = target

    class SchemaRouter:
        def db_for_read(self, model, **hints):
            if model is SchemaDefinition:
                return alias
            return None

    from django.test import override_settings

    try:
        with django_db_blocker.unblock():
            call_command("migrate", database=alias, verbosity=0)
            SchemaDefinition.objects.using(alias).create(resource_type="auth/user")
            definition = SchemaDefinition.objects.using(alias).create(resource_type="blog/post")
            SchemaRelation.objects.using(alias).create(
                definition=definition, name="owner", allowed_subjects=[{"type": "auth/user"}]
            )
            permission = SchemaPermission.objects.using(alias).create(
                definition=definition, name="write", expression="nil"
            )
            ct = ContentType.objects.db_manager(alias).get_for_model(SchemaPermission)
            override = SchemaOverride.objects.using(alias).create(
                kind="loosen",
                target_ct=ct,
                target_pk=permission.pk,
                expression="owner",
                reason="test",
            )
            assert _check(local, resource)
            with override_settings(DATABASE_ROUTERS=[SchemaRouter()]):
                assert check_schema_generation(databases=[alias]) == []
                SchemaGeneration.objects.using(alias).all().delete()
                assert [e.id for e in check_schema_generation(databases=[alias])] == ["rebac.E012"]
                assert check_schema_generation(databases=["default"]) == []
                # A schema write recreates the witness via the installed trigger.
                permission.save(using=alias)
                assert _check(local, resource)
                # Both the override's target and all schema components must
                # come from this alias even though only the owner is routed.
                override.expression = "nil"
                override.save(using=alias)
                assert not _check(local, resource)
                with CaptureQueriesContext(target) as queries:
                    assert not _check(local, resource)
                assert len(queries) == 1
            assert _check(local, resource)
    finally:
        target.close()
        del connections[alias]


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("all_triggers", [False, True])
def test_missing_triggers_with_row_disable_runtime_cache(synced, monkeypatch, scoped, all_triggers):
    _, resource = synced
    permission = _permission()
    migration = import_module("rebac.migrations.0005_schema_generation")
    with connection.schema_editor() as editor:
        if all_triggers:
            migration.uninstall(apps, editor)
        else:
            editor.execute('DROP TRIGGER "rebac_schemapermission_update_generation"')
    # Simulate the first witnessed load in a new worker, without a system check.
    monkeypatch.setattr("rebac.schema.generation._trigger_checks", {})
    local = LocalBackend()
    other = connection.copy(alias="triggerless_writer")
    try:
        revision = _revision()
        with evaluator_scope() if scoped else nullcontext() as evaluator:

            def check():
                if evaluator is not None:
                    return evaluator.check(local, subject=ACTOR, action="write", resource=resource)
                return local.check_access(subject=ACTOR, action="write", resource=resource)

            assert check().allowed
            _raw_permission("nil", permission.pk, using=other)
            assert _revision() == revision
            assert not check().allowed
            assert not check().allowed
            assert local._schema_snapshots == local._schema_facts_memo == {}
            if evaluator is not None:
                assert evaluator.stats()["check_entries"] == 0
    finally:
        BaseDatabaseWrapper.close(other)
        with connection.schema_editor() as editor:
            migration.uninstall(apps, editor)
            SchemaGeneration.objects.all().delete()
            migration.install(apps, editor)


def test_schema_signal_evicts_shared_snapshot_even_when_trigger_breaks(synced):
    local, resource = synced
    assert _check(local, resource)
    before = _revision()
    migration = import_module("rebac.migrations.0005_schema_generation")
    with connection.schema_editor() as editor:
        migration.uninstall(apps, editor)
    try:
        with evaluator_scope() as evaluator:
            assert evaluator.check(local, subject=ACTOR, action="write", resource=resource).allowed
            assert local._schema_snapshots
            assert local._schema_facts_memo
            permission = _permission()
            permission.expression = "nil"
            permission.save(update_fields=["expression"])
            assert _revision() == before
            assert local._schema_snapshots == local._schema_facts_memo == {}
            assert not evaluator.check(
                local, subject=ACTOR, action="write", resource=resource
            ).allowed
    finally:
        with connection.schema_editor() as editor:
            SchemaGeneration.objects.all().delete()
            migration.install(apps, editor)


@pytest.mark.parametrize("gap", ["row", "table", "triggers"])
@pytest.mark.parametrize("scoped", [False, True])
def test_degraded_mode_loads_once_per_operation(synced, monkeypatch, gap, scoped):
    _, resource = synced
    migration = import_module("rebac.migrations.0005_schema_generation")
    with connection.schema_editor() as editor:
        migration.uninstall(apps, editor)
        if gap == "table":
            editor.delete_model(SchemaGeneration)
    if gap == "row":
        SchemaGeneration.objects.all().delete()
    monkeypatch.setattr("rebac.schema.generation._trigger_checks", {})
    local = LocalBackend()
    monkeypatch.setattr("rebac.backends._backend", local)
    try:
        _check(local, resource)  # Detect the gap once, before measuring steady state.
        with evaluator_scope() if scoped else nullcontext() as evaluator:
            surfaces = [
                (lambda: _check(local, resource), 6),
                (lambda: list(Post.objects.with_actor(ACTOR).with_action("write")), 6),
                (
                    lambda: check_new(
                        subject=ACTOR,
                        action="write",
                        resource_type="blog/post",
                        relationships={"owner": [ACTOR]},
                        backend=local,
                    ),
                    5,
                ),
            ]
            if evaluator is not None:
                surfaces.append(
                    (
                        lambda: evaluator.check(
                            local, subject=ACTOR, action="write", resource=resource
                        ),
                        6,
                    )
                )
            for operation, cost in surfaces:
                for n in (1, 10):
                    with CaptureQueriesContext(connection) as queries:
                        for _ in range(n):
                            operation()
                    assert len(queries) == cost * n
                    assert _loads(queries) == n
                    assert _generation_reads(queries) == 0
        assert local._schema_snapshots == local._schema_facts_memo == {}
    finally:
        with connection.schema_editor() as editor:
            if gap == "table":
                editor.create_model(SchemaGeneration)
            SchemaGeneration.objects.all().delete()
            migration.install(apps, editor)


def test_shared_snapshots_and_facts_retain_only_latest_revision(synced):
    local, resource = synced
    permission = _permission()
    old = local._schema_snapshot()
    for index in range(51):
        _raw_permission("nil" if index % 2 else "owner", permission.pk)
        with evaluator_scope() as evaluator:
            assert evaluator.check(
                local, subject=ACTOR, action="write", resource=resource
            ).allowed == (index % 2 == 0)
        assert len(local._schema_snapshots) == len(local._schema_facts_memo) == 1
    latest = next(iter(local._schema_snapshots.values()))
    assert latest.generation != old.generation
    local._schema_facts(old)  # An older in-flight pin cannot repopulate the memo.
    assert set(local._schema_facts_memo) == {latest.generation}


def test_trigger_catalog_checked_once_across_backends_and_threads(synced, monkeypatch):
    _, resource = synced
    from rebac.schema import generation

    monkeypatch.setattr(generation, "_trigger_checks", {})
    inspect = generation.missing_schema_triggers
    calls = []
    barrier = Barrier(4)

    def count(connection):
        calls.append(connection.alias)
        return inspect(connection)

    def worker():
        try:
            barrier.wait(timeout=5)
            return _check(LocalBackend(), resource)
        finally:
            BaseDatabaseWrapper.close(connections["default"])

    monkeypatch.setattr(generation, "missing_schema_triggers", count)
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert all(executor.map(lambda _: worker(), range(4)))
    assert calls == ["default"]


@pytest.mark.parametrize("attribute", ["sqlstate", "pgcode"])
def test_missing_table_detection_supports_both_postgresql_drivers(monkeypatch, attribute):
    from django.db import ProgrammingError
    from django.db.models import QuerySet

    cause = Exception("undefined table")
    setattr(cause, attribute, "42P01")

    def missing(self):
        raise ProgrammingError("undefined table") from cause

    monkeypatch.setattr(QuerySet, "first", missing)
    assert LocalBackend()._read_schema_revision(connection) is None
