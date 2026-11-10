"""Selective child reads must not enumerate the actor's whole visible parent set.

``make test-pg-scale`` runs alone. REBAC_SCALE_MESSAGES/PARTS control the size;
REBAC_SCALE_REPORT optionally saves the measured SQL, plans and JSON summary.
"""

import json
import os
import re
from datetime import timedelta
from pathlib import Path
from statistics import median
from time import perf_counter

import pytest
from django.db import connection
from django.db.models import Count, Exists, OuterRef
from django.utils import timezone

from rebac import MissingActorError, ObjectRef, RelationshipTuple, SubjectRef, to_object_ref
from rebac.models import RebacResource, active_relationship_model
from rebac.models.generation import SchemaGeneration
from rebac.testing import install_schema
from rebac.types import CheckResult
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
    sql, _ = rows.query.sql_with_params()
    assert list(rows) == [{"fragment_id": n, "uses": 1} for n in range(1, 105)]
    assert "COALESCE((SELECT" in sql
    assert '"testapp_scopepart"."id"' in sql
    assert "IS NOT NULL" in sql  # scalar target witness, not parent-set membership
    if not shared:
        assert not re.search(r'UNION(?: ALL)? SELECT "[^"]+"\."id"', sql)
    if connection.vendor == "postgresql":
        plan = json.loads(rows.explain(format="json", analyze=True))[0]["Plan"]
        scans = [n for n in _nodes(plan) if n.get("Relation Name", "").startswith("testapp_scope")]
        assert scans
        assert max(n["Actual Rows"] * n["Actual Loops"] for n in scans) <= 104
        hashed = {
            number
            for node in _nodes(plan)
            for number in re.findall(r"hashed SubPlan (\d+)", node.get("Filter", ""))
        }
        # Hashing tuple membership is fine; hashing a whole visible parent
        # table defeats the caller's selective filter.
        for node in _nodes(plan):
            if (
                node.get("Subplan Name") in {f"SubPlan {number}" for number in hashed}
                and node["Actual Loops"]
            ):
                assert not any(
                    child.get("Relation Name", "").startswith("testapp_scope")
                    for child in _nodes(node)
                )


def _small_rows():
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=n, thread_id=1) for n in range(1, 4)])
    return ScopePart._base_manager.bulk_create(
        [ScopePart(message_id=n, fragment_id=n) for n in range(1, 4)]
    )


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
def test_embedded_scopes_share_one_policy_fence(storage, mixed_actors, settings):
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
    sql, _ = rows.query.sql_with_params()
    assert sql.count('FROM "rebac_schemageneration"') == 1
    assert all(
        row.allowed_0 and row.allowed_1 and row.allowed_2 == (not mixed_actors) for row in rows
    )
    if connection.vendor == "postgresql":
        plan = json.loads(rows.explain(format="json", analyze=True))[0]["Plan"]
        generations = [
            n for n in _nodes(plan) if n.get("Relation Name") == "rebac_schemageneration"
        ]
        assert len(generations) == 1
        assert generations[0]["Actual Loops"] == 1


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
def test_scalar_target_witness_checks_every_row_of_a_nonunique_identity(
    storage, settings, monkeypatch
):
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
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_selective_child_scale(storage, settings):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL scale benchmark")
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    _policy()
    messages = int(os.environ.get("REBAC_SCALE_MESSAGES", "150000"))
    parts = int(os.environ.get("REBAC_SCALE_PARTS", "1500000"))
    assert messages >= 105 and parts >= messages
    # Fixture generation only: no permission SQL is handwritten.
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
        for model in (ScopeThread, ScopeMessage, ScopePart):
            cursor.execute(f"ANALYZE {connection.ops.quote_name(model._meta.db_table)}")
        cursor.execute("SET LOCAL statement_timeout = '90s'")
        cursor.execute("SHOW work_mem")
        work_mem = cursor.fetchone()[0]
    report = {
        "storage": storage,
        "messages": messages,
        "parts": parts,
        "postgres": connection.pg_version,
        "work_mem": work_mem,
        "queries": {},
    }
    output = os.environ.get("REBAC_SCALE_REPORT")

    def cases():
        for actor, prefix, visible in (
            (ADMIN, "", range(1, 105)),
            (MEMBER, "member-", (1, 2)),
            (SubjectRef.of("auth/user", "denied"), "denied-", ()),
        ):
            for name, rows in _queries(actor).items():
                yield prefix + name, rows, visible
        install_schema(_shared_schema(SCHEMA))
        for name, rows in _queries().items():
            yield "shared-" + name, rows, range(1, 105)
        for name, rows in _queries(embedded=True).items():
            yield "embedded-" + name, rows, range(1, 105)

    for name, rows, visible in cases():
        start = perf_counter()
        scoped = rows.scoped()
        sql, params = scoped.query.sql_with_params()
        compile_ms = (perf_counter() - start) * 1000
        durations = []
        for _ in range(3):
            start = perf_counter()
            result = list(scoped.all())
            durations.append((perf_counter() - start) * 1000)
        expected = (
            [{"fragment_id": n, "uses": 1} for n in visible]
            if name.endswith("part_uses")
            else [{"id": n, "message_id": n} for n in list(visible)[:50]]
        )
        assert result == expected
        options = {"memory": True} if connection.pg_version >= 170000 else {}
        explained = json.loads(
            scoped.explain(format="json", analyze=True, buffers=True, **options)
        )[0]
        nodes = list(_nodes(explained["Plan"]))
        report["queries"][name] = {
            "compile_ms": round(compile_ms, 3),
            "wall_ms": round(median(durations), 3),
            "planning_ms": explained["Planning Time"],
            "execution_ms": explained["Execution Time"],
            "sql_bytes": len(sql.encode()),
            "ctes": len(re.findall(r'"rebac_ids_[0-9a-f]+" AS', sql)),
            "plan_nodes": len(nodes),
            "generation_scans": sum(
                n.get("Relation Name") == "rebac_schemageneration" for n in nodes
            ),
            "generation_reads": sum(
                n["Actual Loops"]
                for n in nodes
                if n.get("Relation Name") == "rebac_schemageneration"
            ),
            "largest_scan_rows": max(
                (
                    n.get("Actual Rows", 0)
                    + n.get("Rows Removed by Filter", 0)
                    + n.get("Rows Removed by Index Recheck", 0)
                )
                * n.get("Actual Loops", 0)
                for n in nodes
            ),
            "planner_memory_kb": explained.get("Planning", {}).get("Memory Used"),
            "jit": explained.get("JIT"),
        }
        if output:
            prefix = Path(output)
            prefix.mkdir(parents=True, exist_ok=True)
            (prefix / f"{storage}-{name}.sql").write_text(sql + "\n" + repr(params) + "\n")
            (prefix / f"{storage}-{name}.json").write_text(json.dumps(explained, indent=2) + "\n")
            (prefix / f"{storage}-{name}-query.json").write_text(
                json.dumps({"sql": sql, "params": params}, indent=2) + "\n"
            )
        assert explained["Execution Time"] < 1000, report["queries"][name]
        assert median(durations) < 1000, report["queries"][name]
        assert report["queries"][name]["largest_scan_rows"] <= 104, report["queries"][name]
        assert report["queries"][name]["generation_reads"] == 1
        if (baseline := os.environ.get("REBAC_SCALE_BASELINE")) and "-" not in name:
            query = json.loads((Path(baseline) / f"{storage}-{name}-query.json").read_text())
            memory = ", MEMORY" if connection.pg_version >= 170000 else ""
            with connection.cursor() as cursor:
                cursor.execute(query["sql"], query["params"])
                assert [
                    dict(zip([c[0] for c in cursor.description], row, strict=True))
                    for row in cursor.fetchall()
                ] == expected
                cursor.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON" + memory + ") " + query["sql"],
                    query["params"],
                )
                before = cursor.fetchone()[0][0]
            report["queries"][name]["baseline"] = {
                "execution_ms": before["Execution Time"],
                "planning_ms": before["Planning Time"],
                "planner_memory_kb": before.get("Planning", {}).get("Memory Used"),
            }
            if output:
                (Path(output) / f"{storage}-{name}-baseline.json").write_text(
                    json.dumps(before, indent=2) + "\n"
                )
    print("SCOPE_SCALE " + json.dumps(report, sort_keys=True))
    if output:
        (Path(output) / f"{storage}-summary.json").write_text(
            json.dumps(report, sort_keys=True, indent=2) + "\n"
        )
