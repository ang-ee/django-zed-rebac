"""Selective and broad child reads, shared targets, and embedding plan budgets.

``make test-pg-scale`` runs alone. REBAC_SCALE_MESSAGES/PARTS control the size;
REBAC_SCALE_REPORT selects a persistent report directory; otherwise pytest's
``tmp_path`` holds the measured SQL, plans and JSON summaries.
"""

import json
import os
import re
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from statistics import median
from time import perf_counter

import pytest
from django.db import connection
from django.db.models import Count, Exists, OuterRef
from django.utils import timezone

from rebac import (
    MissingActorError,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    evaluator_scope,
    to_object_ref,
)
from rebac.models import RebacResource, active_relationship_model
from rebac.models.generation import SchemaGeneration
from rebac.testing import install_schema
from rebac.types import CheckResult
from tests.scope_scale_limits import (
    assert_plan_identity,
    assert_query_identity,
    assert_rescan_failure,
    known_limitation,
)
from tests.testapp.models import Folder, Post, ScopeMessage, ScopePart, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
ADMIN = SubjectRef.of("auth/user", "admin")
MEMBER = SubjectRef.of("auth/user", "member")
SCHEMA = """
definition auth/user {}
definition auth/group { relation member: auth/user | auth/group#member }
definition scope/role {
    relation direct: auth/user | auth/group#member
    relation includes: scope/role
    permission member = direct + includes->member
}
definition scope/thread {
    relation admin: scope/role // rebac:const=admin
    relation direct: auth/user
    relation group: auth/group#member
    permission read = (admin->member + direct) + group
}
definition scope/message {
    relation admin: scope/role // rebac:const=admin
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    relation group: auth/group#member
    permission read = ((admin->member + direct) + group) + thread->read
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
"""


def _shared_schema(schema):
    return schema.replace(
        "permission read = message->read",
        "permission read = message->read\npermission inspect = message->read",
    )


def _policy(*, shared=False):
    local = install_schema(_shared_schema(SCHEMA) if shared else SCHEMA)
    local.write_relationships(
        [
            RelationshipTuple(
                ObjectRef("scope/role", "admin"), "includes", SubjectRef.of("scope/role", "staff")
            ),
            RelationshipTuple(ObjectRef("scope/role", "staff"), "direct", ADMIN),
            RelationshipTuple(ObjectRef("auth/group", "inner"), "member", MEMBER),
            RelationshipTuple(
                ObjectRef("auth/group", "outer"),
                "member",
                SubjectRef.of("auth/group", "inner", "member"),
            ),
            RelationshipTuple(
                ObjectRef("scope/thread", "1"),
                "group",
                SubjectRef.of("auth/group", "outer", "member"),
            ),
            RelationshipTuple(ObjectRef("scope/message", "2"), "direct", MEMBER),
        ]
    )
    return local


def _queries(actor=ADMIN, *, embedded=False):
    rows = ScopePart.objects.with_actor(actor).filter(fragment_id__in=range(1, 105))
    if embedded:
        rows = rows.filter(Exists(ScopePart.objects.with_actor(actor).filter(pk=OuterRef("pk"))))
    return {
        "part_uses": rows.order_by()
        .values("fragment_id")
        .annotate(uses=Count("message_id", distinct=True))
        .order_by("fragment_id"),
        "listing": rows.order_by("id").values("id", "message_id")[:50],
    }


def _nodes(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _nodes(child)


def _analyze():
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            for model in (
                ScopeThread,
                ScopeMessage,
                ScopePart,
                active_relationship_model(),
                RebacResource,
                SchemaGeneration,
            ):
                cursor.execute(f"ANALYZE {connection.ops.quote_name(model._meta.db_table)}")


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("shared", [False, True])
def test_selective_child_scope_plan(storage, shared, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    _policy(shared=shared)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=1) for n in range(1, 206)])
    ScopePart._base_manager.bulk_create(
        [ScopePart(message_id=n, fragment_id=n) for n in range(1, 206)]
    )
    _analyze()
    rows = _queries()["part_uses"].scoped()
    assert list(rows) == [{"fragment_id": n, "uses": 1} for n in range(1, 105)]


def _small_rows():
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=1) for n in range(1, 4)])
    return ScopePart._base_manager.bulk_create(
        [ScopePart(message_id=n, fragment_id=n) for n in range(1, 4)]
    )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_shared_field_target_keeps_its_caller_alias(storage, depth, settings):
    """An embedded recursive field arrow still names the enclosing row."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation parent: blog/folder // rebac:field=parent
            relation viewer: auth/user
            permission local = viewer
            permission read = parent->read + parent->local
            permission inspect = parent->local
        }
    """)
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("blog/folder", "1"), "viewer", MEMBER),
        ]
    )
    Folder._base_manager.bulk_create(
        [
            Folder(id=1),
            Folder(id=2, parent_id=1),
            Folder(id=3, parent_id=2),
            Folder(id=4),
        ]
    )
    rows = Folder.objects.with_actor(MEMBER).scoped()
    for _ in range(depth):
        rows = Folder._base_manager.filter(Exists(rows.filter(pk=OuterRef("pk"))))
    assert list(rows.order_by("pk").values_list("pk", flat=True)) == [2, 3]


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize(
    "expression,expected",
    [
        ("admin->member + blocked", [1, 2, 3]),
        ("admin->member - blocked", [1, 3]),
        ("admin->member & approved", [1, 2]),
        ("(admin->member - blocked) & approved", [1]),
    ],
)
def test_universal_arm_algebra_keeps_conditional_bans(
    storage, shared, expression, expected, settings
):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    text = SCHEMA.replace(
        "permission read = ((admin->member + direct) + group) + thread->read",
        "relation blocked: auth/user | auth/user with enabled with expiration\n"
        "relation approved: auth/user:*\npermission read = " + expression,
    )
    local = install_schema(
        "use expiration\ncaveat enabled(ok bool) { ok }\n"
        + (_shared_schema(text) if shared else text)
    )
    parts = _small_rows()
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("scope/role", "admin"), "direct", ADMIN),
            RelationshipTuple(
                ObjectRef("scope/message", "2"), "blocked", ADMIN, caveat_name="enabled"
            ),
            RelationshipTuple(
                ObjectRef("scope/message", "3"),
                "blocked",
                ADMIN,
                expires_at=timezone.now() - timedelta(seconds=1),
            ),
            *(
                RelationshipTuple(
                    ObjectRef("scope/message", str(n)), "approved", SubjectRef.of("auth/user", "*")
                )
                for n in (1, 2)
            ),
        ]
    )
    assert (
        list(
            ScopePart.objects.with_actor(ADMIN)
            .order_by("fragment_id")
            .values_list("fragment_id", flat=True)
        )
        == expected
    )
    for part in parts:
        answer = local.check_access(subject=ADMIN, action="read", resource=to_object_ref(part))
        assert answer.allowed == (part.fragment_id in expected)
        if part.fragment_id == 2 and " - " in expression:
            assert answer == CheckResult.conditional(["ok"])
    from tests.reference_harness import assert_scope_matches

    assert_scope_matches(ScopePart, actor=ADMIN, action="read")
    with pytest.raises(MissingActorError):
        ScopePart.objects.exists()


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("shared,pure", [(False, False), (True, False), (True, True)])
def test_parent_universal_witness_observes_revocation_and_expiration(
    storage, shared, pure, settings, monkeypatch
):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    schema = "use expiration\n" + SCHEMA.replace(
        "relation direct: auth/user | auth/group#member",
        "relation direct: auth/user | auth/group#member with expiration",
    )
    if pure:
        schema = schema.replace(
            "permission read = ((admin->member + direct) + group) + thread->read",
            "permission read = admin->member",
        )
    local = install_schema(_shared_schema(schema) if shared else schema)
    _small_rows()
    now = timezone.now()
    deadline = now + timedelta(minutes=1)
    edge = RelationshipTuple(ObjectRef("scope/role", "admin"), "direct", ADMIN, expires_at=deadline)
    local.write_relationships([edge])
    scoped = ScopePart.objects.with_actor(ADMIN).scoped()
    assert scoped.count() == 3
    local.delete_relationship(edge)
    assert not scoped.exists()
    local.write_relationships([edge])
    assert scoped.count() == 3
    monkeypatch.setattr("django.utils.timezone.now", lambda: deadline)
    assert not scoped.exists()


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("mixed_actors", [False, True])
def test_embedded_scopes_preserve_each_actor(storage, mixed_actors, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    _policy()
    _small_rows()
    _analyze()
    rows = ScopePart.objects.with_actor(ADMIN)
    for n in range(3):
        actor = SubjectRef.of("auth/user", "denied") if mixed_actors and n == 2 else ADMIN
        rows = rows.annotate(
            **{
                f"allowed_{n}": Exists(
                    ScopeMessage.objects.with_actor(actor).filter(pk=OuterRef("message_id"))
                )
            }
        )
    rows = rows.scoped()
    assert all(
        row.allowed_0 and row.allowed_1 and row.allowed_2 == (not mixed_actors) for row in rows
    )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("same_role", [False, True])
def test_uniform_fallback_does_not_assume_a_different_role_or_caveat_bound(
    storage, same_role, settings
):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    schema = (
        _shared_schema(SCHEMA)
        .replace(
            "relation direct: auth/user | auth/group#member",
            "relation direct: auth/user | auth/user with enabled | auth/group#member",
        )
        .replace(
            "permission read = (admin->member + direct) + group",
            "permission read = authenticated - admin->member",
        )
        .replace(
            "permission read = ((admin->member + direct) + group) + thread->read",
            "permission read = admin->member + thread->read",
        )
    )
    if not same_role:
        schema = schema.replace("rebac:const=admin", "rebac:const=other", 1)
    local = install_schema("caveat enabled(ok bool) { ok }\n" + schema)
    parts = _small_rows()
    conditional = RelationshipTuple(
        ObjectRef("scope/role", "admin"), "direct", ADMIN, caveat_name="enabled"
    )
    local.write_relationships([conditional])
    rows = ScopePart.objects.with_actor(ADMIN).order_by("fragment_id").scoped()
    assert list(rows.values_list("fragment_id", flat=True)) == ([] if same_role else [1, 2, 3])
    for part in parts:
        answer = local.check_access(subject=ADMIN, action="read", resource=to_object_ref(part))
        if same_role:
            assert answer == CheckResult.conditional(["ok"])
        else:
            assert answer.allowed
    local.delete_relationship(conditional)
    # The same prepared statement now takes the inherited negative path.
    assert list(rows.values_list("fragment_id", flat=True)) == [1, 2, 3]
    local.write_relationships(
        [RelationshipTuple(ObjectRef("scope/role", "admin"), "direct", ADMIN)]
    )
    assert list(rows.values_list("fragment_id", flat=True)) == [1, 2, 3]


@pytest.mark.parametrize("levels", [4, pytest.param(9, marks=pytest.mark.slow)])
def test_field_arrow_diamond_keeps_shared_predicate_definitions(levels):
    def schema(branching):
        lines = [
            "definition auth/user {}",
            "definition blog/folder {",
            "relation parent: blog/folder // rebac:field=parent",
            "relation viewer: auth/user",
        ]
        for n in range(levels):
            expression = f"viewer + parent->p{n + 1}"
            if branching:
                expression = f"({expression}) + parent->q{n + 1}"
            lines += [f"permission p{n} = {expression}", f"permission q{n} = p{n} + viewer"]
        lines += [
            f"permission p{levels} = viewer",
            f"permission q{levels} = viewer",
            "permission read = p0",
            "}",
        ]
        return "\n".join(lines)

    install_schema(schema(False))
    single, _ = Folder.objects.with_actor(ADMIN).scoped().query.sql_with_params()
    local = install_schema(schema(True))
    Folder._base_manager.bulk_create(
        [
            Folder(id=n, name=f"level-{n}", parent_id=n + 1 if n <= levels else None)
            for n in range(1, levels + 2)
        ]
    )
    local.write_relationships(
        [RelationshipTuple(ObjectRef("blog/folder", str(levels + 1)), "viewer", ADMIN)]
    )
    rows = Folder.objects.with_actor(ADMIN).filter(name="level-1").scoped()
    sql, _ = rows.query.sql_with_params()
    assert len(re.findall(r'"rebac_ids_[0-9a-f]+" AS', sql)) >= levels - 1
    assert " AS NOT MATERIALIZED (" not in sql
    # Branching doubles the paths at each level, but not the emitted text.
    assert len(sql) < 3 * len(single)
    assert len(sql) < 3000 * (levels + 1)
    assert list(rows.values_list("pk", flat=True)) == [1]
    assert not Folder.objects.with_actor(MEMBER).filter(name="level-1").exists()
    if connection.vendor == "postgresql" and connection.pg_version >= 170000:
        explained = json.loads(rows.explain(format="json", memory=True))[0]
        assert explained["Planning"]["Memory Used"] < 8192
        print(
            "SCOPE_DIAMOND "
            + json.dumps(
                {
                    "levels": levels,
                    "sql_bytes": len(sql.encode()),
                    "planner_memory_kb": explained["Planning"]["Memory Used"],
                },
                sort_keys=True,
            )
        )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_field_target_checks_every_row_of_a_nonunique_identity(storage, settings, monkeypatch):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    monkeypatch.setattr(Folder._meta, "rebac_id_attr", "name", raising=False)
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation member: auth/user // rebac:const={"target_id":"admin","filters":{"is_active":true}}
            permission read = member
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
            permission excluded = authenticated - folder->read
        }
    """)
    first, _, other = Folder._base_manager.bulk_create(
        [
            Folder(name="shared", is_active=False),
            Folder(name="shared", is_active=True),
            Folder(name="other", is_active=False),
        ]
    )
    inherited, hidden = Post._base_manager.bulk_create(
        [Post(title="shared identity", folder=first), Post(title="other", folder=other)]
    )
    for action, expected in (("read", inherited), ("excluded", hidden)):
        assert list(
            Post.objects.with_actor(ADMIN).with_action(action).values_list("pk", flat=True)
        ) == [expected.pk]
        for post in (inherited, hidden):
            assert local.check_access(
                subject=ADMIN, action=action, resource=to_object_ref(post)
            ).allowed == (post.pk == expected.pk)


@pytest.mark.scale
@pytest.mark.slow
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("scope_mode", ["outside", "inside"])
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("shape", ["single", "shared", "arrow_only"])
def test_selective_child_scale(storage, shape, scope_mode, settings, subtests, tmp_path):
    _scale_matrix(storage, shape, scope_mode, settings, subtests, tmp_path)


@pytest.mark.scale
@pytest.mark.slow
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("scope_mode", ["outside", "inside"])
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_scope_embedding_planning(storage, scope_mode, settings, subtests, tmp_path):
    _scale_matrix(storage, "single", scope_mode, settings, subtests, tmp_path, planning_only=True)


def _scale_matrix(storage, shape, scope_mode, settings, subtests, tmp_path, *, planning_only=False):
    """Warm, deterministic narrow/broad matrix; timings are evidence, not gates."""
    from django.db import DatabaseError, transaction

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL scale benchmark")
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    _policy()
    schema = SCHEMA
    if shape == "arrow_only":
        schema = schema.replace(
            "permission read = ((admin->member + direct) + group) + thread->read",
            "permission read = thread->read",
        )
    if shape == "shared":
        schema = _shared_schema(schema)
    install_schema(schema)
    messages = int(os.environ.get("REBAC_SCALE_MESSAGES", "50000"))
    parts = int(os.environ.get("REBAC_SCALE_PARTS", "500000"))
    assert messages >= 105 and parts >= messages
    team = SubjectRef.of("auth/user", "team")
    large = SubjectRef.of("auth/user", "large")
    grants = (
        [RelationshipTuple(ObjectRef("auth/group", "team"), "member", team)]
        + [
            RelationshipTuple(
                ObjectRef("scope/thread", str(n)),
                "group",
                SubjectRef.of("auth/group", "team", "member"),
            )
            for n in range(20, messages + 1, 20)
        ]
        + [
            RelationshipTuple(ObjectRef("scope/thread", str(n)), "direct", large)
            for n in range(2, messages + 1, 2)
        ]
    )
    model = active_relationship_model()
    refs = {}
    if storage == "registry":
        refs = RebacResource.upsert_refs_bulk(
            sorted(
                {(t.resource.resource_type, t.resource.resource_id) for t in grants}
                | {(t.subject.subject_type, t.subject.subject_id) for t in grants}
            ),
            using="default",
        )
    tuples = []
    for grant in grants:
        fields = {
            "relation": grant.relation,
            "optional_subject_relation": grant.subject.optional_relation,
        }
        if storage == "registry":
            fields.update(
                resource_fk_id=refs[(grant.resource.resource_type, grant.resource.resource_id)],
                subject_fk_id=refs[(grant.subject.subject_type, grant.subject.subject_id)],
            )
        else:
            fields.update(
                resource_type=grant.resource.resource_type,
                resource_id=grant.resource.resource_id,
                subject_type=grant.subject.subject_type,
                subject_id=grant.subject.subject_id,
            )
        tuples.append(model(**fields))
    model._base_manager.bulk_create(tuples, batch_size=1000)
    # Fixture generation only; permission SQL always comes from Django.
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO testapp_scopethread (id, label) "
            "SELECT i, 'thread' FROM generate_series(1, %s) i",
            [messages],
        )
        cursor.execute(
            "INSERT INTO testapp_scopemessage (id, thread_id) "
            "SELECT i, i FROM generate_series(1, %s) i",
            [messages],
        )
        cursor.execute(
            "INSERT INTO testapp_scopepart (id, message_id, fragment_id) "
            "SELECT i, 1 + (i - 1) %% %s, i FROM generate_series(1, %s) i",
            [messages, parts],
        )
        cursor.execute("SHOW jit")
        default_jit = cursor.fetchone()[0]
        cursor.execute("SHOW work_mem")
        work_mem = cursor.fetchone()[0]
        cursor.execute("SET statement_timeout = '30s'")
    _analyze()
    output = Path(os.environ.get("REBAC_SCALE_REPORT", tmp_path))
    output.mkdir(parents=True, exist_ok=True)
    baseline_path = os.environ.get("REBAC_SCALE_BASELINE")
    baseline = (
        json.loads(
            (Path(baseline_path) / f"{storage}-{shape}-{scope_mode}-summary.json").read_text()
        )
        if baseline_path
        else None
    )
    report = {
        "storage": storage,
        "shape": shape,
        "scope_mode": scope_mode,
        "messages": messages,
        "parts": parts,
        "postgres": connection.pg_version,
        "work_mem": work_mem,
        "default_jit": default_jit,
        "queries": {},
        "planning": {},
    }

    suffix = "planning-summary" if planning_only else "summary"

    def save():
        (output / f"{storage}-{shape}-{scope_mode}-{suffix}.json").write_text(
            json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        )

    class Captured(Exception):
        pass

    def capture(build_rows, count=False):
        statement = None
        probes = []

        def intercept(execute, sql, params, many, context):
            nonlocal statement
            if '"testapp_scopepart"' in sql:
                statement = (sql, params)
                raise Captured
            start = perf_counter()
            result = execute(sql, params, many, context)
            probes.append(
                {"sql": sql, "params": params, "wall_ms": (perf_counter() - start) * 1000}
            )
            return result

        with evaluator_scope() if scope_mode == "inside" else nullcontext():
            with connection.execute_wrapper(intercept):
                with pytest.raises(Captured):
                    rows = build_rows()
                    rows.count() if count else list(rows)
                first = len(probes)
                first_statement = statement
                with pytest.raises(Captured):
                    rows = build_rows()
                    rows.count() if count else list(rows)
                assert statement == first_statement
        assert statement is not None
        preparation = {
            "first_statement_count": first + 1,
            "repeat_statement_count": len(probes) - first + 1,
            "first_witness_count": sum("EXISTS" in p["sql"] for p in probes[:first]),
            "repeat_witness_count": sum("EXISTS" in p["sql"] for p in probes[first:]),
            "first_probe_ms": sum(p["wall_ms"] for p in probes[:first]),
            "probe_statements": probes,
        }
        return *statement, preparation

    violations = []
    for jit in (default_jit, "off"):
        with connection.cursor() as cursor:
            cursor.execute("SET jit = " + jit)
        actors = (
            ()
            if planning_only
            else (("admin", ADMIN), ("member", MEMBER), ("team", team), ("large", large))
        )
        for name, actor in actors:
            visible = {
                n
                for n in range(1, messages + 1)
                if name == "admin"
                or (name == "member" and n in ((1,) if shape == "arrow_only" else (1, 2)))
                or (name == "team" and n % 20 == 0)
                or (name == "large" and n % 2 == 0)
            }
            for narrow in (True, False):
                ids = [
                    n
                    for n in range(1, (104 if narrow else parts) + 1)
                    if 1 + (n - 1) % messages in visible
                ]
                for count in (True, False):
                    key = f"{jit}-{name}-{'narrow' if narrow else 'broad'}-{'count' if count else 'listing'}"

                    def build_rows(actor=actor, narrow=narrow, count=count):
                        rows = ScopePart.objects.with_actor(actor)
                        if narrow:
                            rows = rows.filter(fragment_id__in=range(1, 105))
                        if shape == "shared":
                            rows = rows.filter(
                                Exists(
                                    ScopePart.objects.with_actor(actor).filter(pk=OuterRef("pk"))
                                )
                            )
                        if not count:
                            rows = rows.order_by("id").values_list("id", "message_id")[:50]
                        return rows

                    started = perf_counter()
                    sql, params, preparation = capture(build_rows, count)
                    entry = {
                        "compile_ms": round((perf_counter() - started) * 1000, 3),
                        **preparation,
                    }
                    expected = (
                        [(len(ids),)] if count else [(n, 1 + (n - 1) % messages) for n in ids[:50]]
                    )
                    entry["expected_rows"] = expected
                    report["queries"][key] = entry
                    (output / f"{storage}-{shape}-{scope_mode}-{key}-query.json").write_text(
                        json.dumps({"sql": sql, "params": params}, sort_keys=True, indent=2) + "\n"
                    )
                    if baseline_path:
                        before_query = json.loads(
                            (
                                Path(baseline_path)
                                / f"{storage}-{shape}-{scope_mode}-{key}-query.json"
                            ).read_text()
                        )
                        entry["baseline_sql_equal"] = sql == before_query["sql"]
                        entry["baseline_params_equal"] = list(params) == before_query["params"]
                        if name != "admin" or scope_mode == "outside":
                            assert entry["baseline_sql_equal"]
                            assert entry["baseline_params_equal"]
                    known = known_limitation(report, key)
                    if known is not None:
                        assert_query_identity(known, sql, params)
                    if known is not None and known["kind"] == "timeout":
                        # The owner permits bounding these eight measured timeout cells.
                        # EXPLAIN does not execute the query: no row/timing claim is made.
                        with connection.cursor() as cursor:
                            cursor.execute("EXPLAIN (FORMAT JSON) " + sql, params)
                            explained = cursor.fetchone()[0][0]
                        assert_plan_identity(known, explained["Plan"])
                        entry.update(stage="bounded-known-timeout", known_limitation=known)
                        (output / f"{storage}-{shape}-{scope_mode}-{key}-plan.json").write_text(
                            json.dumps(explained, sort_keys=True, indent=2) + "\n"
                        )
                        save()
                        with subtests.test(fixture=f"{storage}-{shape}-{scope_mode}", cell=key):
                            pytest.xfail(known["reason"])
                        continue
                    save()
                    try:
                        with transaction.atomic(), connection.cursor() as cursor:
                            entry["stage"] = "warmup"
                            cursor.execute(sql, params)  # warm-up, excluded
                            assert cursor.fetchall() == expected
                            durations = []
                            entry["stage"] = "samples"
                            for _ in range(3):
                                started = perf_counter()
                                cursor.execute(sql, params)
                                result = cursor.fetchall()
                                durations.append((perf_counter() - started) * 1000)
                                assert result == expected
                            entry.update(
                                wall_ms=round(median(durations), 3),
                                samples_ms=[round(x, 3) for x in durations],
                                rows=result,
                                stage="explain",
                            )
                            save()
                            memory = ", MEMORY" if connection.pg_version >= 170000 else ""
                            cursor.execute(
                                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON" + memory + ") " + sql,
                                params,
                            )
                            explained = cursor.fetchone()[0][0]
                    except DatabaseError as error:
                        entry["error"] = str(error)
                        reason = (
                            f"{type(error).__name__}: {str(error).strip().splitlines()[0][:240]}"
                        )
                        violations.append(f"{key}: {entry['stage']}: {reason}")
                        save()
                        print(f"SCOPE_CELL {key} ERROR at {entry['stage']}: {reason}", flush=True)
                        continue
                    nodes = list(_nodes(explained["Plan"]))
                    rescans = [
                        n
                        for n in nodes
                        if n.get("Node Type") == "CTE Scan"
                        and n.get("Actual Loops", 0) > 1
                        and n.get("Actual Rows", 0) + n.get("Rows Removed by Filter", 0) > 1
                    ]
                    entry.update(
                        {
                            "wall_ms": round(median(durations), 3),
                            "samples_ms": [round(x, 3) for x in durations],
                            "planning_ms": explained["Planning Time"],
                            "execution_ms": explained["Execution Time"],
                            "rows": result,
                            "sql_bytes": len(sql.encode()),
                            "planner_memory_kb": explained.get("Planning", {}).get("Memory Used"),
                            "quadratic_cte_rescans": len(rescans),
                            "jit": explained.get("JIT"),
                            "stage": "complete",
                        }
                    )
                    if baseline is not None:
                        before = baseline["queries"][key]
                        if "rows" in before:
                            assert [list(row) for row in result] == before["rows"]
                            entry["baseline_rows_equal"] = True
                        else:
                            entry["baseline_rows_equal"] = None  # baseline timed out
                        if name == "admin" and "wall_ms" in before:
                            if entry["wall_ms"] > max(25, 1.25 * before["wall_ms"]):
                                violations.append(key + ": admin exceeds 1.25x baseline and 25 ms")
                            if scope_mode == "inside" and narrow:
                                if entry["wall_ms"] * 10 > before["wall_ms"]:
                                    violations.append(key + ": admin narrow gain below 10x")
                    if known is not None:
                        assert known["kind"] == "rescan"
                        assert_plan_identity(known, explained["Plan"])
                        # Strict harness equivalent: a fixed or different rescan is a
                        # hard failure outside the xfail context, as is identity drift.
                        assert_rescan_failure(known, len(rescans))
                        entry["known_limitation"] = known
                        with subtests.test(fixture=f"{storage}-{shape}-{scope_mode}", cell=key):
                            pytest.xfail(known["reason"])
                    elif rescans:
                        violations.append(key + ": CTE rescanned per caller row")
                    (output / f"{storage}-{shape}-{scope_mode}-{key}-plan.json").write_text(
                        json.dumps(explained, sort_keys=True, indent=2) + "\n"
                    )
                    save()
                    print("SCOPE_CELL " + key + " " + str(entry["wall_ms"]), flush=True)
        if shape == "single":
            for k in (1, 4, 16):

                def build_rows(k=k):
                    rows = ScopePart.objects.with_actor(team).filter(fragment_id__in=range(1, 105))
                    for _ in range(k):
                        rows = rows.filter(
                            Exists(
                                ScopeMessage.objects.with_actor(team).filter(
                                    pk=OuterRef("message_id")
                                )
                            )
                        )
                    return rows.values_list("id", flat=True)

                sql, params, preparation = capture(build_rows)
                planning_name = f"{storage}-{shape}-{scope_mode}-{jit}-{k}-planning-query.json"
                (output / planning_name).write_text(
                    json.dumps({"sql": sql, "params": params}, sort_keys=True, indent=2) + "\n"
                )
                with connection.cursor() as cursor:
                    memory = ", MEMORY" if connection.pg_version >= 170000 else ""
                    cursor.execute("EXPLAIN (SUMMARY, FORMAT JSON" + memory + ") " + sql, params)
                    explained = cursor.fetchone()[0][0]
                entry = {
                    **preparation,
                    "sql_bytes": len(sql.encode()),
                    "planning_ms": explained.get("Planning Time"),
                    "planner_memory_kb": explained.get("Planning", {}).get("Memory Used"),
                }
                report["planning"][f"{jit}-{k}"] = entry
                if baseline_path:
                    before_query = json.loads((Path(baseline_path) / planning_name).read_text())
                    entry["baseline_sql_equal"] = sql == before_query["sql"]
                    entry["baseline_params_equal"] = list(params) == before_query["params"]
                    assert entry["baseline_sql_equal"] and entry["baseline_params_equal"]
                if connection.pg_version >= 170000 and baseline is not None:
                    before_memory = baseline["planning"][f"{jit}-{k}"]["planner_memory_kb"]
                    entry["baseline_planner_memory_kb"] = before_memory
                    # SQL/parameters were asserted identical above. The accepted
                    # contract records memory variation for identical statements;
                    # this is a measurement, not an independent memory ceiling.
                    entry["planner_memory_delta_kb"] = entry["planner_memory_kb"] - before_memory
                save()
    assert not violations, violations
