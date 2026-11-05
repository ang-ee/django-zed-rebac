"""Actor-side stored sets are decided before and witnessed in statements."""

from datetime import timedelta
from itertools import pairwise

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    evaluator_scope,
    sudo,
)
from rebac._id import resource_id_attr
from rebac.compile import read
from rebac.compile.predicate import Bound
from rebac.compile.program import CompileProgram
from rebac.schema import parse_zed
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
ACTOR = SubjectRef.of("auth/user", "alice")
DOC = ObjectRef("docs/doc", "d")
SCHEMA = """
definition auth/user {}
definition auth/group {
    relation member: auth/user | auth/group#member
}
definition docs/doc {
    relation viewer: auth/user | auth/group#member
    relation banned: auth/group#member
    permission read = viewer - banned
}
"""


def member(name, subject):
    return RelationshipTuple(ObjectRef("auth/group", name), "member", subject)


def group(name):
    return SubjectRef.of("auth/group", name, "member")


def test_stored_set_relation_is_detected():
    assert CompileProgram.build(parse_zed(SCHEMA)).stored_sets == {("auth/group", "member")}


def test_actor_set_decision_reaches_fixed_point_through_cycle(monkeypatch):
    monkeypatch.setattr(read, "_SET_WITNESS_BUDGET", 10**9)
    local = install_schema(SCHEMA)
    names = [f"g{index}" for index in range(12)]
    local.write_relationships(
        [
            member(names[0], ACTOR),
            *(member(outer, group(inner)) for inner, outer in pairwise(names)),
            member(names[0], group(names[-1])),
        ]
    )
    operation = read._Operation.begin(local, ACTOR, "default")
    sets = operation.sets(operation.verdicts(("docs/doc", "read")))
    assert sets is not None
    assert sets.ids(("auth/group", "member"), Bound.LOWER) == frozenset(names)


def test_decided_actor_set_does_not_also_define_subject_cte():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation member: auth/user | blog/folder#member
        }
        definition blog/post {
            relation viewer: blog/folder#member
            permission read = viewer
        }
    """)
    with sudo(reason="decided actor set fixture"):
        folder = Folder.objects.create(name="staff")
    local.write_relationships(
        [RelationshipTuple(ObjectRef("blog/folder", str(folder.pk)), "member", ACTOR)]
    )
    operation = read._Operation.begin(local, ACTOR, "default")
    verdicts = operation.verdicts(("blog/post", "read"))
    sets = operation.sets(verdicts)
    assert sets is not None
    model = operation.model_index.resource("blog/folder")
    assert model is not None
    compiler = operation.compiler(verdicts, sets=sets, share_subject_rows=True)
    assert (
        compiler._subject_cte(
            ("blog/folder", "member"),
            model,
            resource_id_attr(model),
            Bound.LOWER,
            {},
            True,
        )
        is None
    )
    assert compiler.statement_ctes() == ()
    predicate = compiler.holds(("blog/folder", "member"), read._model_at(model), Bound.LOWER)
    matching = compiler.with_subject_ctes(Folder._base_manager.filter(predicate))
    _sql, placeholders = matching.query.sql_with_params()
    params = operation.bound(placeholders, verdicts=verdicts)
    if connection.vendor == "postgresql":
        assert any(folder.pk in param for param in params if isinstance(param, list))
    else:
        assert str(folder.pk) in {str(param) for param in params}
    assert list(matching.values_list("pk", flat=True)) == [folder.pk]


def test_stale_set_decision_cannot_grant(monkeypatch):
    local = install_schema(SCHEMA)
    edge = member("staff", ACTOR)
    local.write_relationships([edge, RelationshipTuple(DOC, "viewer", group("staff"))])
    operation = read._Operation.begin(local, ACTOR, "default")
    stale = operation.sets(operation.verdicts(("docs/doc", "read")))
    assert stale is not None
    local.delete_relationship(edge)
    monkeypatch.setattr(read._Operation, "_decide_sets", lambda self, key, verdicts: stale)
    assert not local.check_access(subject=ACTOR, action="read", resource=DOC).allowed


@pytest.mark.django_db(transaction=True)
def test_evaluator_reuses_actor_set_decision():
    local = install_schema(SCHEMA)
    local.write_relationships(
        [member("staff", ACTOR), RelationshipTuple(DOC, "viewer", group("staff"))]
    )
    with evaluator_scope():
        with CaptureQueriesContext(connection) as first:
            assert local.check_access(subject=ACTOR, action="read", resource=DOC).allowed
        with CaptureQueriesContext(connection) as second:
            assert not local.check_access(
                subject=ACTOR, action="read", resource=ObjectRef("docs/doc", "other")
            ).allowed
    assert len(first) > 1
    # The denied point still needs its lower and upper probes, but no set walk.
    assert len(second) == 2


def test_twelve_deep_membership_reaches_fixed_point_at_default_depth():
    local = install_schema(
        SCHEMA.replace(
            "definition docs/doc {",
            "definition blog/post {",
        )
        .replace("    relation banned: auth/group#member\n", "")
        .replace("    permission read = viewer - banned", "    permission read = viewer")
    )
    with sudo(reason="bounded membership fixture"):
        post = Post.objects.create(title="deep membership")
    names = [f"g{n}" for n in range(12)]
    local.write_relationships(
        [
            member(names[0], ACTOR),
            *(member(outer, group(inner)) for inner, outer in pairwise(names)),
            RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "viewer", group(names[-1])),
        ]
    )
    assert Post.objects.with_actor(ACTOR).filter(pk=post.pk).exists()
    assert local.check_access(
        subject=ACTOR, action="read", resource=ObjectRef("blog/post", str(post.pk))
    ).allowed


def test_nested_sets_and_cycles_are_decided_once(monkeypatch):
    local = install_schema(SCHEMA)
    local.write_relationships(
        [
            member("inner", ACTOR),
            member("outer", group("inner")),
            member("inner", group("outer")),
            RelationshipTuple(DOC, "viewer", group("outer")),
        ]
    )
    with CaptureQueriesContext(connection) as queries:
        assert local.check_access(subject=ACTOR, action="read", resource=DOC).allowed
    # Two levels, the empty frontier, then the point statement.
    assert len(queries) == 4
    decided = max(len(query["sql"].encode()) for query in queries)
    monkeypatch.setattr(read._Operation, "sets", lambda self, verdicts: None)
    read.reset()
    with CaptureQueriesContext(connection) as inline_queries:
        assert local.check_access(subject=ACTOR, action="read", resource=DOC).allowed
    inline = len(inline_queries[-1]["sql"].encode())
    print(f"sets SQL bytes: decided={decided}, inline={inline}")
    assert decided < inline, (decided, inline)


@pytest.mark.pg_delta
def test_actor_set_witness_byte_budget_and_model_identity_boundary(monkeypatch):
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation member: auth/user | blog/folder#member
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->member
        }
    """)
    with sudo(reason="set size fixture"):
        folders = Folder.objects.bulk_create([Folder(name=f"f{i}") for i in range(205)])
        posts = Post.objects.bulk_create(
            [Post(title=f"p{i}", folder=folder) for i, folder in enumerate(folders)]
        )
    key = ("blog/post", "read")

    def measure(count, *, execute=True):
        local.write_relationships(
            [
                RelationshipTuple(ObjectRef("blog/folder", str(folder.pk)), "member", ACTOR)
                for folder in folders[:count]
            ]
        )
        read.reset()
        operation = read._Operation.begin(local, ACTOR, "default")
        sets = operation.sets(operation.verdicts(key))
        if not execute:
            return sets, [], 0, ""
        with evaluator_scope(), CaptureQueriesContext(connection) as queries:
            found = list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True))
        sql = queries[-1]["sql"]
        return sets, found, len(sql.encode()), sql

    one, found_one, one_bytes, _ = measure(1)
    below, found_below, below_bytes, below_sql = measure(4)
    above, found_above, above_bytes, _ = measure(5)
    large, found_large, large_bytes, _ = measure(200)
    assert one is not None and below is not None
    assert above is None and large is None
    assert sorted(found_one) == [posts[0].pk]
    assert sorted(found_below) == sorted(post.pk for post in posts[:4])
    assert sorted(found_above) == sorted(post.pk for post in posts[:5])
    assert sorted(found_large) == sorted(post.pk for post in posts[:200])
    assert (large_bytes - above_bytes) / 195 < 16
    assert large_bytes < 10_000
    assert 350 < (below_bytes - one_bytes) / 3 < 500
    assert above_bytes < 10_000
    assert "CASE WHEN" not in below_sql
    with monkeypatch.context() as patcher:
        patcher.setattr(read, "_SET_WITNESS_BUDGET", 10**9)
        read.reset()
        lifted, lifted_rows, lifted_bytes, lifted_sql = measure(200)
        assert lifted is not None
        assert lifted_rows == [post.pk for post in posts[:200]]
        assert lifted_sql.count("CASE WHEN") == 0
        assert lifted_bytes < 95_000


@pytest.mark.pg_delta
def test_noncanonical_decided_model_identity_matches_inline(monkeypatch):
    monkeypatch.setattr(read, "_SET_WITNESS_BUDGET", 10**9)
    local = install_schema("""
        definition auth/user {}
        definition blog/folder { relation member: auth/user | blog/folder#member }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->member
        }
    """)
    with sudo(reason="noncanonical set fixture"):
        folder = Folder.objects.create(name="canonical")
        Post.objects.create(title="linked", folder=folder)

    def measure(count):
        local.write_relationships(
            [
                RelationshipTuple(
                    ObjectRef("blog/folder", f"{'0' * n}{folder.pk}"), "member", ACTOR
                )
                for n in range(1, count + 1)
            ]
        )
        read.reset()
        operation = read._Operation.begin(local, ACTOR, "default")
        assert operation.sets(operation.verdicts(("blog/post", "read"))) is not None
        with evaluator_scope(), CaptureQueriesContext(connection) as queries:
            found = list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True))
        return found, max(len(query["sql"].encode()) for query in queries), len(queries)

    _, short_bytes, short_statements = measure(1)
    decided, long_bytes, long_statements = measure(10)
    assert short_statements == long_statements
    assert (long_bytes - short_bytes) / 9 < 100
    assert long_bytes < 4_000
    with monkeypatch.context() as patcher:
        patcher.setattr(read._Operation, "sets", lambda self, verdicts: None)
        read.reset()
        inline = list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True))
    assert decided == inline == []


def test_revoked_membership_changes_next_statement():
    local = install_schema(SCHEMA)
    edge = member("staff", ACTOR)
    local.write_relationships([edge, RelationshipTuple(DOC, "viewer", group("staff"))])
    assert local.check_access(subject=ACTOR, action="read", resource=DOC).allowed
    local.delete_relationship(edge)
    assert not local.check_access(subject=ACTOR, action="read", resource=DOC).allowed


def test_new_membership_excludes_in_same_evaluator_scope():
    local = install_schema(SCHEMA)
    local.write_relationships(
        [
            RelationshipTuple(DOC, "viewer", ACTOR),
            RelationshipTuple(DOC, "banned", group("out")),
        ]
    )
    with evaluator_scope():
        assert local.check_access(subject=ACTOR, action="read", resource=DOC).allowed
        local.write_relationships([member("out", ACTOR)])
        assert not local.check_access(subject=ACTOR, action="read", resource=DOC).allowed


def test_membership_appearing_before_statement_excludes_point_and_scope(monkeypatch):
    schema = SCHEMA.replace("definition docs/doc {", "definition blog/post {")
    local = install_schema(schema)
    with sudo(reason="excluded membership fixture"):
        post = Post.objects.create(title="excluded")
    resource = ObjectRef("blog/post", str(post.pk))
    local.write_relationships(
        [
            RelationshipTuple(resource, "viewer", ACTOR),
            RelationshipTuple(resource, "banned", group("out")),
        ]
    )
    operation = read._Operation.begin(local, ACTOR, "default")
    stale = operation.sets(operation.verdicts(("blog/post", "read")))
    assert stale is not None
    local.write_relationships([member("out", ACTOR)])
    monkeypatch.setattr(read._Operation, "_decide_sets", lambda self, key, verdicts: stale)
    assert not local.check_access(subject=ACTOR, action="read", resource=resource).allowed
    assert not Post.objects.with_actor(ACTOR).filter(pk=post.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_wildcard_caveat_and_expiry_shape_the_two_bounds(monkeypatch):
    import rebac.clock

    local = install_schema("""
        use expiration
        caveat active(ok bool) { ok }
        definition auth/user {}
        definition docs/doc {
            relation viewer: auth/user:* with active with expiration
            permission read = viewer
        }
    """)
    now = timezone.now()
    deadline = now + timedelta(minutes=1)
    local.write_relationships(
        [
            RelationshipTuple(
                DOC,
                "viewer",
                SubjectRef.of("auth/user", "*"),
                caveat_name="active",
                expires_at=deadline,
            )
        ]
    )
    monkeypatch.setattr(rebac.clock, "application_now", lambda: now)
    undecided = local.check_access(subject=ACTOR, action="read", resource=DOC)
    assert undecided.conditional_on == ("ok",)
    assert local.check_access(
        subject=ACTOR, action="read", resource=DOC, context={"ok": True}
    ).allowed
    monkeypatch.setattr(rebac.clock, "application_now", lambda: deadline)
    assert not local.check_access(
        subject=ACTOR, action="read", resource=DOC, context={"ok": True}
    ).allowed
