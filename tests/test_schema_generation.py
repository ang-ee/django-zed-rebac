"""Persisted revisions bound schema reads without caching live authorization rows."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from io import StringIO
from threading import Barrier, Event

import pytest
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
from rebac.models import (
    SchemaCaveat,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
)
from rebac.models.generation import SchemaGeneration

from .backend_setup import STORAGE_TIERS
from .testapp.models import Folder, Post

pytestmark = pytest.mark.django_db(transaction=True)
ACTOR = SubjectRef.of("auth/user", "1")


@pytest.fixture(params=["denormalized", "registry"])
def synced(request, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    call_command("rebac", "sync", stdout=StringIO())
    SchemaDefinition.objects.create(resource_type="auth/user")
    with sudo(reason="schema revision fixture"):
        post = Post.objects.create(title="cached schema")
    reset_backend()
    local = backend()
    resource = ObjectRef("blog/post", str(post.pk))
    local.write_relationships([RelationshipTuple(resource, "owner", ACTOR)])
    local.schema()  # Warm the per-revision snapshot before measuring scoped reads.
    yield local, resource
    reset_backend()


def _check(local, resource):
    return local.check_access(subject=ACTOR, action="write", resource=resource).allowed


def _revision():
    return SchemaGeneration.objects.get(pk=1).revision


def _loads(queries):
    return sum('FROM "rebac_schemadefinition"' in query["sql"] for query in queries)


def _generation_reads(queries):
    # Count standalone witness round trips, not the execution fence embedded
    # in the permission statement (which adds no database round trip).
    return sum(query["sql"].startswith('SELECT "rebac_schemageneration".') for query in queries)


def _permission():
    return SchemaPermission.objects.get(definition__resource_type="blog/post", name="write")


def _write_permission(expression, pk, *, using=connection):
    # Copied connections need registration for Django's transaction owner.
    if using is connection:
        using = connections["default"]
    original = connections[using.alias] if using.alias in connections else None
    connections[using.alias] = using
    try:
        SchemaPermission.objects.using(using.alias).filter(pk=pk).update(expression=expression)
    finally:
        if original is None:
            del connections[using.alias]
        else:
            connections[using.alias] = original


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
            assert _loads(repeated) == 0
            assert _generation_reads(repeated) == 0
            if expected:
                assert all("rebac_schemageneration" in query["sql"] for query in repeated)


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
def test_absent_witness_fails_closed_until_rebuilt(synced, scoped):
    from rebac import SchemaError
    from tests.backend_setup import rebuild_backend

    local, resource = synced
    SchemaGeneration.objects.all().delete()
    local.mark_schema_stale()
    with evaluator_scope() if scoped else nullcontext() as evaluator:
        with pytest.raises(SchemaError, match="E013"):
            if evaluator is None:
                _check(local, resource)
            else:
                evaluator.check(local, subject=ACTOR, action="write", resource=resource)
        if evaluator is not None:
            assert evaluator.stats()["check_entries"] == 0
    rebuild_backend(local)
    assert _check(local, resource)


def test_revision_reads_can_run_concurrently(synced):
    local, resource = synced
    barrier = Barrier(4)

    def instrument(execute, sql, params, many, context):
        if sql.startswith('SELECT "rebac_schemageneration".'):
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

    witness = SchemaGeneration.objects.witness

    def changing_revision(using):
        reads.append(using)
        current = witness(using)
        # Three racing before/after pairs, then a stable paired fallback.
        if (len(reads) - 1) % 8 < 6:
            return current._replace(revision=str(len(reads)))
        return current

    load_snapshot = local._load_schema_snapshot

    def racing_snapshot():
        # Interleave this reader's witness observations only. Writes and other
        # backends must continue to use their real publication witnesses.
        with monkeypatch.context() as reader_patch:
            reader_patch.setattr(SchemaGeneration.objects, "witness", changing_revision)
            return load_snapshot()

    monkeypatch.setattr(local, "_load_schema_snapshot", racing_snapshot)
    load = local._load_schema_from_db
    loads = []

    def count_load(using, **kwargs):
        if kwargs.get("overrides") is not None:
            return load(using, **kwargs)
        loads.append(using)
        return load(using)

    monkeypatch.setattr(local, "_load_schema_from_db", count_load)
    snapshot = local._schema_snapshot()
    assert (snapshot.revision, snapshot.index_revision, snapshot.index_program) == witness(
        "default"
    )
    assert snapshot.generation == -1
    assert len(reads) == 8  # Six racing reads plus the two fallback witnesses.
    assert len(loads) == 4  # Three attempts, then one uncached fallback.
    assert local._schema_loads == local._schema_snapshots == {}
    with evaluator_scope() as evaluator:
        assert evaluator.check(local, subject=ACTOR, action="write", resource=resource).allowed
        _write_permission("nil", _permission().pk)
        assert not evaluator.check(local, subject=ACTOR, action="write", resource=resource).allowed
        assert evaluator.stats()["check_entries"] == 0
    assert local._schema_snapshots == local._schema_facts_memo == {}


def test_generation_model_is_private():
    import rebac.models

    assert not hasattr(rebac.models, "SchemaGeneration")
    assert SchemaGeneration._meta.default_permissions == ()


def test_uncached_fallback_retains_mismatched_index_witness(synced, monkeypatch):
    from unittest.mock import Mock

    from rebac.errors import SchemaError
    from rebac.index import read

    local = LocalBackend()
    current = SchemaGeneration.objects.witness("default")
    revision, stale = current.revision, "stale-index"
    pairs = Mock(
        side_effect=[current._replace(revision=str(n), index_revision=stale) for n in range(6)]
        + [current._replace(index_revision=stale)] * 2
    )
    monkeypatch.setattr(SchemaGeneration.objects, "witness", pairs)
    with read.using_backend(local):
        snapshot = local._schema_snapshot()
        assert snapshot.generation == -1
        assert (snapshot.revision, snapshot.index_revision) == (revision, stale)
        with pytest.raises(SchemaError, match=r"rebac\.E013"):
            read.ensure_ready(using="default")
    assert pairs.call_count == 8  # Readiness consumes the pinned pair, with no ninth SELECT.
    assert local._schema_snapshots == local._schema_facts_memo == {}


@pytest.mark.parametrize("external", [False, True])
def test_sync_invalidates_existing_backend_on_next_check(synced, monkeypatch, external):
    local, resource = synced
    permission = _permission()
    _write_permission("viewer", permission.pk)
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


def test_noop_sync_publishes_revision(synced):
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
    _write_permission("viewer", permission.pk)
    original = _revision()
    assert not _check(local, resource)
    with transaction.atomic():
        _write_permission("owner", permission.pk)
        temporary = _revision()
        assert _check(local, resource)
        transaction.set_rollback(True)
    assert _revision() == original
    # No intervening permission read after rollback: a counter could reuse the
    # temporary revision here and incorrectly grant from its cached AST.
    with transaction.atomic():
        _write_permission("viewer", permission.pk)
        assert _revision() not in (original, temporary)
        assert not _check(local, resource)
    assert not _check(local, resource)


@pytest.mark.parametrize("boundary", ["atomic", "savepoint", "manual"])
def test_rollback_without_evaluator_discards_temporary_schema(synced, boundary):
    local, resource = synced
    permission = _permission()
    _write_permission("viewer", permission.pk)
    assert not _check(local, resource)
    if boundary == "manual":
        transaction.set_autocommit(False)
        try:
            _write_permission("owner", permission.pk)
            assert _check(local, resource)
            transaction.rollback()
            assert not _check(local, resource)
        finally:
            transaction.rollback()
            transaction.set_autocommit(True)
    else:
        with transaction.atomic():
            savepoint = transaction.savepoint() if boundary == "savepoint" else None
            _write_permission("owner", permission.pk)
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

    def racing_load(using, **kwargs):
        if kwargs.get("overrides") is not None:
            return load(using, **kwargs)
        result = load(using)
        calls.append(using)
        if len(calls) == 1:
            _write_permission("viewer", permission.pk, using=other)
        return result

    monkeypatch.setattr(local, "_load_schema_from_db", racing_load)
    try:
        assert not _check(local, resource)
        assert calls == ["default", "default"]
        assert not _check(local, resource)
        assert len(calls) == 2
    finally:
        BaseDatabaseWrapper.close(other)


def test_shared_backend_threads_parse_once(synced, monkeypatch):
    _, resource = synced
    local = LocalBackend()
    barrier = Barrier(8)
    loads = []
    load = local._load_schema_from_db

    def count_load(using, **kwargs):
        if kwargs.get("overrides") is not None:
            return load(using, **kwargs)
        loads.append(using)
        return load(using)

    monkeypatch.setattr(local, "_load_schema_from_db", count_load)

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
    assert len(loads) == local._schema_generation == 1


def test_signal_invalidation_does_not_wait_on_database_loader(synced, monkeypatch):
    _, resource = synced
    local = LocalBackend()
    entered, release = Event(), Event()
    load = local._load_schema_from_db
    calls = []

    def slow_load(using, **kwargs):
        if kwargs.get("overrides") is not None:
            return load(using, **kwargs)
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
    model.objects.filter(pk=row.pk).delete()
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
        call_command("migrate", "rebac", verbosity=0)
    assert list(SchemaPermission.objects.order_by("pk").values()) == before
    from tests.backend_setup import rebuild_backend

    rebuild_backend(local)
    assert _revision() != revision
    assert _check(local, resource)
    _write_permission("viewer", _permission().pk)
    assert not _check(local, resource)


@pytest.mark.parametrize("synced", STORAGE_TIERS, indirect=True)
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
        Folder.objects.sudo(reason="backing fixture update").filter(pk=row.pk).update(
            is_active=False
        )
        with CaptureQueriesContext(connection) as queries:
            assert not evaluator.check(
                local, subject=ACTOR, action="read", resource=resource
            ).allowed
            assert not Post.objects.with_actor(ACTOR).filter(pk=post.pk).exists()
        assert _loads(queries) == 0
        assert _revision() == revision
        Folder.objects.sudo(reason="backing fixture update").filter(pk=row.pk).update(
            is_active=True
        )
        assert evaluator.check(local, subject=ACTOR, action="read", resource=resource).allowed
        Post.objects.sudo(reason="backing fixture update").filter(pk=post.pk).update(folder=None)
        assert not evaluator.check(local, subject=ACTOR, action="read", resource=resource).allowed


@pytest.mark.parametrize("synced", STORAGE_TIERS, indirect=True)
def test_schema_and_override_cache_follow_routed_database(synced, tmp_path, django_db_blocker):
    local, resource = synced
    alias = "schema_target"
    from tests.backend_setup import sqlite_alias

    target = sqlite_alias(alias, tmp_path / "schema.sqlite3")
    connections[alias] = target

    class SchemaRouter:
        def db_for_read(self, model, **hints):
            return alias if model._meta.app_label == "rebac" else None

        def db_for_write(self, model, **hints):
            return alias if model._meta.app_label == "rebac" else None

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
                local.write_relationships([RelationshipTuple(resource, "owner", ACTOR)])
                assert _check(local, resource)
                # Both the override's target and all schema components must
                # come from the same routed alias as the index and source tuples.
                override.expression = "nil"
                override.save(using=alias)
                assert not _check(local, resource)
                with CaptureQueriesContext(target) as queries:
                    assert not _check(local, resource)
                assert len(queries) == 2
                SchemaGeneration.objects.using(alias).all().delete()
                assert local._read_schema_revision(target) is None
            assert _check(local, resource)
    finally:
        target.close()
        del connections[alias]


@pytest.mark.parametrize("synced", STORAGE_TIERS, indirect=True)
def test_sync_uses_one_schema_write_alias(synced, tmp_path, django_db_blocker):
    from django.test import override_settings

    alias = "schema_sync_target"
    from tests.backend_setup import sqlite_alias

    target = sqlite_alias(alias, tmp_path / "sync.sqlite3")
    connections[alias] = target

    class SchemaRouter:
        def db_for_write(self, model, **hints):
            return alias if model is SchemaDefinition else None

    try:
        with django_db_blocker.unblock():
            call_command("migrate", database=alias, verbosity=0)
            before = _revision()
            target_before = (
                SchemaGeneration.objects.using(alias)
                .filter(pk=1)
                .values_list("revision", flat=True)
                .first()
            )
            with override_settings(DATABASE_ROUTERS=[SchemaRouter()]):
                call_command("rebac", "sync", stdout=StringIO())
                call_command("rebac", "sync", "--check", stdout=StringIO())
            assert SchemaPermission.objects.using(alias).filter(name="read").exists()
            assert SchemaGeneration.objects.using(alias).get(pk=1).revision != target_before
            assert _revision() == before
    finally:
        target.close()
        del connections[alias]


@pytest.mark.parametrize("writes", [3, pytest.param(51, marks=pytest.mark.slow)])
@pytest.mark.parametrize("synced", STORAGE_TIERS, indirect=True)
def test_shared_snapshots_retain_only_latest_revision(synced, writes):
    local, resource = synced
    permission = _permission()
    old = local._schema_snapshot()
    for index in range(writes):
        _write_permission("nil" if index % 2 else "owner", permission.pk)
        with evaluator_scope() as evaluator:
            assert evaluator.check(
                local, subject=ACTOR, action="write", resource=resource
            ).allowed == (index % 2 == 0)
        assert len(local._schema_snapshots) == 1
    latest = next(iter(local._schema_snapshots.values()))
    assert latest.generation != old.generation
