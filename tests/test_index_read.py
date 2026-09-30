"""Read semantics and structural budgets for the materialized index.

Direct storage fixtures isolate the reader; harness cases also exercise the
source-to-index contract against the frozen walker and reference model.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.db.models import Exists, OuterRef, Prefetch, Subquery
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, backend, sudo
from rebac.actors import anonymous_actor
from rebac.errors import MissingActorError, SchemaError
from rebac.evaluator import evaluator_scope
from rebac.index import conditions, read
from rebac.index import time as index_time
from rebac.index.program import program_for
from rebac.index.terms import AUTHENTICATED, anonymous, intern, type_level
from rebac.index.time import index_now
from rebac.models.generation import SchemaGeneration
from rebac.models.index import IndexCover, IndexMember, IndexState, IndexTerm
from rebac.schema import parse_zed
from rebac.types import PermissionResult
from tests import index_harness
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db

SCHEMA = """
caveat first(a bool) { a }
caveat second(b bool) { b }
definition auth/user {}
definition auth/anonymous {}
definition auth/group {
    relation member: auth/user | auth/user:* | auth/user with first | auth/user with second | auth/group#member
}
definition blog/folder {
    relation viewer: auth/user | auth/group#member
    permission read = viewer
}
definition blog/post {
    relation viewer: auth/user | auth/user:* | auth/group#member | auth/user with first | auth/user with second
    relation blocked: auth/user | auth/group#member
    relation parent: blog/post
    permission walk = viewer + parent->walk
    permission read = walk - blocked
    permission public = authenticated - blocked
    permission create = authenticated
}
definition virtual/item {
    relation viewer: auth/user | auth/group#member
    permission read = viewer
}
"""


@pytest.fixture(params=("denormalized", "registry"))
def active(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        current = backend()
        current.set_schema(parse_zed(SCHEMA))
        SchemaGeneration.objects.update_or_create(
            pk=1,
            defaults={
                "revision": current._manual_schema_revision(),
                "index_revision": current._manual_schema_revision(),
                "index_program": program_for(current, using="default").digest,
            },
        )
        IndexState.objects.get_or_create(key="global")
        yield current


def term(type_, object_id, relation=""):
    triple = type_, object_id, relation
    return intern([triple], using="default")[triple]


def member(holder, actor, condition=None, **interval):
    return IndexMember.objects.create(
        set_id=holder,
        member_id=actor,
        member_type=IndexTerm.objects.get(pk=actor).type,
        condition=condition,
        condition_key=conditions.key(condition),
        **interval,
    )


def cover(
    holder,
    *,
    resource="1",
    type_="blog/post",
    condition=None,
    action="read",
    **interval,
):
    scope = term(*type_level(type_)) if resource is None else term(type_, resource)
    return IndexCover.objects.create(
        scope_id=scope,
        resource_type=type_,
        node=action,
        holder_id=holder,
        site="",
        condition=condition,
        condition_key=conditions.key(condition),
        **interval,
    )


def site_of(type_, node):
    """The one site a node holds, by the name the program gives it."""
    (site,) = program_for(backend(), using="default").held_sites((type_, node))
    return site[1]


def add_ban(parent, holder, *, scope=None, condition=None, **expiry):
    """Populate the named site in the test schema from source operand rows."""
    IndexCover.objects.create(
        scope_id=parent.scope_id,
        resource_type=parent.resource_type,
        node="walk",
        holder_id=parent.holder_id,
        site="",
        expires_at=parent.expires_at,
        condition=parent.condition,
        condition_key=parent.condition_key,
    )
    parent.holder_id = parent.scope_id
    parent.site = site_of(parent.resource_type, parent.node)
    parent.condition = None
    parent.condition_key = ""
    parent.save()
    return IndexCover.objects.create(
        scope_id=scope or parent.scope_id,
        resource_type=parent.resource_type,
        node="blocked",
        holder_id=holder,
        site="",
        condition=condition,
        condition_key=conditions.key(condition),
        **expiry,
    )


def check(actor, resource="1", **kwargs):
    return read.check(
        resource=ObjectRef("blog/post", resource),
        actor=actor,
        action=kwargs.pop("action", "read"),
        context=kwargs.pop("context", None),
        using="default",
        **kwargs,
    )


@pytest.mark.parametrize(
    "wire,authenticated,wild",
    [
        ("auth/user:alice", True, True),
        ("auth/user:", False, True),
        ("auth/user:alice#member", True, False),
        ("auth/user:#member", False, False),
        ("auth/anonymous:*", False, True),
        ("auth/anonymous:other", True, True),
    ],
)
def test_actor_sets_shape_and_classes(active, wire, authenticated, wild):
    actor = SubjectRef.parse(wire)
    exact = term(actor.subject_type, actor.subject_id, actor.optional_relation)
    auth = term(*AUTHENTICATED)
    wildcard = term(actor.subject_type, "*")
    sets = read.actor_sets(actor, using="default", now=index_now())
    assert set(sets.exact) == {exact}
    definite = set(sets.definite)
    assert (auth in definite) is authenticated
    assert (wildcard in definite) is wild
    assert set(sets.possible) == definite


def test_unknown_actor_matches_wildcard_memberships_and_classes(active):
    wildcard = term("auth/user", "*")
    auth = term(*AUTHENTICATED)
    inner = term("auth/group", "inner", "member")
    outer = term("auth/group", "outer", "member")
    member(inner, wildcard)
    member(outer, wildcard)
    member(outer, inner)
    actor = SubjectRef.of("auth/user", "not-interned")
    sets = read.actor_sets(actor, using="default", now=index_now())
    assert set(sets.exact) == set()
    assert {wildcard, auth, inner, outer} <= set(sets.definite)
    subject_set = read.actor_sets(
        SubjectRef.of("auth/user", "not-interned", "member"), using="default", now=index_now()
    )
    assert set(subject_set.definite) == {auth}


def test_conditional_and_expired_membership_sets(active):
    actor = term("auth/user", "alice")
    definite = term("auth/group", "definite", "member")
    possible = term("auth/group", "possible", "member")
    expired = term("auth/group", "expired", "member")
    now = index_now()
    member(definite, actor)
    member(possible, actor, conditions.leaf("first", {}))
    member(expired, actor, expires_at=now)
    sets = read.actor_sets(SubjectRef.of("auth/user", "alice"), using="default", now=now)
    assert set(sets.definite) == {actor, definite}
    assert set(sets.possible) == {actor, definite, possible}


def test_anonymous_class_is_the_singleton(active):
    anon = term(*anonymous())
    cover(anon)
    assert check(anonymous_actor()).allowed
    assert not check(SubjectRef.of(anonymous_actor().subject_type, "other")).allowed


def test_cover_exceptions_do_not_cancel_independent_grant(active):
    alice = term("auth/user", "alice")
    group = term("auth/group", "staff", "member")
    member(group, alice)
    broad = cover(term(*AUTHENTICATED))
    add_ban(broad, group)
    actor = SubjectRef.of("auth/user", "alice")
    assert not check(actor).allowed
    cover(alice)
    assert check(actor).allowed


def test_all_conditional_membership_paths_and_missing_sets(active):
    alice = term("auth/user", "alice")
    group = term("auth/group", "staff", "member")
    member(group, alice, conditions.leaf("first", {}))
    member(group, alice, conditions.leaf("second", {}))
    cover(group)
    actor = SubjectRef.of("auth/user", "alice")
    result = check(actor)
    assert result.result is PermissionResult.CONDITIONAL_PERMISSION
    assert result.conditional_on == ("a", "b")
    assert check(actor, context={"a": False}).conditional_on == ("b",)
    assert check(actor, context={"a": False, "b": True}).allowed
    assert check(actor, context={"a": False, "b": False}).result is PermissionResult.NO_PERMISSION


def test_conditional_exception_memberships_are_possible_denials(active):
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    alice = term("auth/user", "alice")
    group = term("auth/group", "blocked", "member")
    member(group, alice, conditions.leaf("first", {}))
    member(group, alice, conditions.leaf("second", {}))
    add_ban(cover(term(*AUTHENTICATED)), group)
    actor = SubjectRef.of("auth/user", "alice")
    assert check(actor).conditional_on == ("a", "b")
    assert check(actor, context={"a": False, "b": False}).allowed
    assert not check(actor, context={"a": False, "b": True}).allowed
    assert (
        list(
            Post._base_manager.filter(
                read.scope_q(Post, action="read", actor=actor, using="default")
            )
        )
        == []
    )


def test_type_level_cover_concrete_exception_and_empty_resource(active):
    Post._base_manager.bulk_create([Post(pk=1, title="one"), Post(pk=2, title="two")])
    term("blog/post", "2")
    alice = term("auth/user", "alice")
    add_ban(cover(alice, resource=None), alice, scope=term("blog/post", "1"))
    actor = SubjectRef.of("auth/user", "alice")
    assert not check(actor, "1").allowed
    assert check(actor, "2").allowed
    assert check(actor, "").allowed
    assert set(
        read.accessible_ids(resource_type="blog/post", action="read", actor=actor, using="default")
    ) == {"2"}
    IndexCover.objects.all().delete()
    cover(alice, resource="1")
    assert check(actor, "").allowed


@pytest.mark.pg_delta
def test_intervals_are_half_open(active, monkeypatch):
    now = index_now()
    actor = SubjectRef.of("auth/user", "alice")
    grant = cover(term("auth/user", "alice"), expires_at=now + timedelta(days=1))
    monkeypatch.setattr(index_time, "index_now", lambda: now)
    assert check(actor).allowed
    deny = add_ban(grant, grant.holder_id, expires_at=now)
    assert check(actor).allowed
    deny.expires_at = now + timedelta(days=1)
    deny.save()
    assert not check(actor).allowed
    monkeypatch.setattr(index_time, "index_now", lambda: now + timedelta(days=1))
    assert not check(actor).allowed


def test_check_in_reach_of_a_caveat_reads_its_rows_in_one_statement(active):
    alice = term("auth/user", "alice")
    group = term("auth/group", "staff", "member")
    for caveat in ("first", "second"):
        member(group, alice, conditions.leaf(caveat, {}))
    cover(group)
    with CaptureQueriesContext(connection) as queries:
        result = check(SubjectRef.of("auth/user", "alice"))
    index_queries = [q["sql"] for q in queries if "rebac_grant" in q["sql"]]
    # The rows of the whole plan, the site's operands included, in one read.
    assert len(index_queries) == 1
    assert index_queries[0].count(" UNION ALL ") == 2
    assert result.conditional_on == ("a", "b")
    cover(term(*AUTHENTICATED), resource=None, action="create")
    with CaptureQueriesContext(connection) as queries:
        assert check(SubjectRef.of("auth/user", "alice"), action="create").allowed
    # No caveat is in reach of `create`, so the statement decides.
    assert ["EXISTS" in q["sql"] for q in queries if "rebac_grant" in q["sql"]] == [True]


def test_model_less_ids_and_context_enumeration(active):
    alice = term("auth/user", "alice")
    cover(alice, type_="virtual/item", resource="external")
    assert set(
        active.accessible(
            subject=SubjectRef.of("auth/user", "alice"), action="read", resource_type="virtual/item"
        )
    ) == {"external"}
    cover(alice, resource="1", condition=conditions.leaf("first", {}))
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    assert (
        list(
            active.accessible(
                subject=SubjectRef.of("auth/user", "alice"),
                action="read",
                resource_type="blog/post",
            )
        )
        == []
    )
    assert list(
        active.accessible(
            subject=SubjectRef.of("auth/user", "alice"),
            action="read",
            resource_type="blog/post",
            context={"a": True},
        )
    ) == ["1"]


def test_lookup_subjects_expands_nested_members_and_excludes_denials(active):
    index_harness.seed(
        [
            "auth/group:inner#member@auth/user:alice",
            "auth/group:outer#member@auth/group:inner#member",
            "auth/group:outer#member@auth/user:bob",
            "blog/post:parent#viewer@auth/group:outer#member",
            "blog/post:child#parent@blog/post:parent",
            "blog/post:child#blocked@auth/user:bob",
        ],
        backend=active,
    )
    resource = ObjectRef("blog/post", "child")
    assert set(
        active.lookup_subjects(resource=resource, action="read", subject_type="auth/user")
    ) == {SubjectRef.of("auth/user", "alice")}
    index_harness.assert_index_matches(
        subjects=[SubjectRef.of("auth/user", name) for name in ("alice", "bob", "outsider")],
        resources=[resource],
        actions=["read"],
    )


def test_lookup_subjects_context(active):
    alice = term("auth/user", "alice")
    cover(alice, condition=conditions.leaf("first", {}))
    args = dict(resource=ObjectRef("blog/post", "1"), action="read", subject_type="auth/user")
    assert list(active.lookup_subjects(**args)) == []
    assert list(active.lookup_subjects(**args, context={"a": True})) == [
        SubjectRef.of("auth/user", "alice")
    ]


def test_harness_conditional_paths_and_wildcard_groups(active):
    active.write_relationships(
        [
            RelationshipTuple(
                ObjectRef("auth/group", "conditional"),
                "member",
                SubjectRef.of("auth/user", "alice"),
                caveat_name=name,
            )
            for name in ("first", "second")
        ]
    )
    index_harness.seed(
        [
            "auth/group:everyone#member@auth/user:*",
            "blog/post:conditional#viewer@auth/group:conditional#member",
            "blog/post:public#viewer@auth/group:everyone#member",
            "blog/post:public#blocked@auth/group:conditional#member",
        ],
        backend=active,
    )
    index_harness.assert_index_matches(
        subjects=[
            SubjectRef.of("auth/user", "alice"),
            SubjectRef.of("auth/user", "unknown"),
            SubjectRef.of("auth/group", "conditional", "member"),
            anonymous_actor(),
        ],
        resources=[ObjectRef("blog/post", "conditional"), ObjectRef("blog/post", "public")],
        actions=["read"],
        contexts=[None, {"a": False}, {"a": False, "b": True}, {"a": False, "b": False}],
    )


@pytest.mark.parametrize("revision", [None, "2" * 32])
def test_readiness_mismatch_fails_closed(active, revision):
    SchemaGeneration.objects.filter(pk=1).update(index_revision=revision)
    actor = SubjectRef.of("auth/user", "alice")
    for operation in (
        lambda: read.ensure_ready(using="default"),
        lambda: check(actor),
        lambda: read.scope_q(Post, action="read", actor=actor, using="default"),
        lambda: active.accessible(subject=actor, action="read", resource_type="blog/post"),
        lambda: active.lookup_subjects(
            resource=ObjectRef("blog/post", "1"), action="read", subject_type="auth/user"
        ),
    ):
        with pytest.raises(SchemaError, match=r"rebac\.E013"):
            operation()


def test_readiness_missing_row_fails_closed(active):
    SchemaGeneration.objects.filter(pk=1).delete()
    with pytest.raises(SchemaError, match=r"rebac\.E013"):
        read.ensure_ready(using="default")


def test_generation_manager_publishes_current_revision_in_one_statement(active):
    SchemaGeneration.objects.filter(pk=1).update(revision="3" * 32, index_revision=None)
    assert SchemaGeneration.objects.revision_pair("default") == ("3" * 32, None)
    assert not SchemaGeneration.objects.filter(SchemaGeneration.objects.ready_q()).exists()
    with CaptureQueriesContext(connection) as queries:
        assert SchemaGeneration.objects.publish_index("default", program="4" * 32) == 1
    assert len(queries) == 1
    assert queries[0]["sql"].lstrip().upper().startswith("UPDATE ")
    assert SchemaGeneration.objects.witness("default") == ("3" * 32, "3" * 32, "4" * 32)
    assert SchemaGeneration.objects.filter(SchemaGeneration.objects.ready_q()).exists()
    SchemaGeneration.objects.filter(pk=1).update(revision="", index_revision="")
    assert not SchemaGeneration.objects.filter(SchemaGeneration.objects.ready_q()).exists()


@pytest.mark.pg_delta
def test_identity_filter_rejects_noncanonical_integer_spelling(active):
    from rebac._id import model_identity_filter

    row = Post(pk=1, title="canonical")
    Post._base_manager.bulk_create([row])
    assert Post._base_manager.filter(model_identity_filter(Post, "pk", "1")).get() == row
    for wire in ("01", "+1", " 1", "1\n", "\u0661", "*", "", str(2**100)):
        assert not Post._base_manager.filter(model_identity_filter(Post, "pk", wire)).exists()


def test_warm_backend_reuses_its_revision_read_for_readiness(active, monkeypatch):
    from rebac.backends.local import LocalBackend

    database_backend = LocalBackend()
    monkeypatch.setattr(
        database_backend,
        "_load_schema_from_db",
        lambda using, overrides=(): (active.schema(), None),
    )
    actor = SubjectRef.of("auth/user", "alice")
    resource = ObjectRef("blog/post", "1")
    cover(term("auth/user", "alice"))
    database_backend.check_access(subject=actor, resource=resource, action="read")
    with CaptureQueriesContext(connection) as queries:
        result = database_backend.check_access(subject=actor, resource=resource, action="read")
    revision_reads = [
        q["sql"] for q in queries if q["sql"].startswith('SELECT "rebac_schemageneration".')
    ]
    assert result.allowed
    assert len(revision_reads) == 1, [q["sql"][:180] for q in queries]
    assert "index_revision" in revision_reads[0]
    assert len(queries) == 2


@pytest.mark.django_db(transaction=True)
def test_decision_cache_observes_owned_revoke(active):
    actor = SubjectRef.of("auth/user", "alice")
    resource = ObjectRef("blog/post", "1")
    from rebac.types import RelationshipFilter

    active.write_relationships([RelationshipTuple(resource, "viewer", actor)])
    with evaluator_scope() as evaluator:
        assert evaluator.check(active, subject=actor, action="read", resource=resource).allowed
        active.delete_relationships(
            RelationshipFilter(resource_type="blog/post", resource_id="1", relation="viewer")
        )
        assert not evaluator.check(active, subject=actor, action="read", resource=resource).allowed


@pytest.mark.parametrize("expired_part", ["cover", "member", "exception"])
def test_reused_scoped_queryset_binds_time_at_execution(active, monkeypatch, expired_part):
    instant = index_now()
    deadline = instant + timedelta(hours=1)
    monkeypatch.setattr(index_time, "index_now", lambda: instant)
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    actor = SubjectRef.of("auth/user", "alice")
    alice = term("auth/user", "alice")
    if expired_part == "member":
        group = term("auth/group", "g", "member")
        member(group, alice, expires_at=deadline)
        cover(group)
    elif expired_part == "exception":
        add_ban(
            cover(alice, resource=None), alice, scope=term("blog/post", "1"), expires_at=deadline
        )
    else:
        cover(alice, expires_at=deadline)
    scoped = Post.objects.with_actor(actor).scoped()
    assert list(scoped.values_list("pk", flat=True)) == ([] if expired_part == "exception" else [1])
    instant = deadline
    assert list(scoped.values_list("pk", flat=True)) == ([1] if expired_part == "exception" else [])


def test_unknown_type_and_action_preserve_reason(active):
    actor = SubjectRef.of("auth/user", "alice")
    assert (
        read.check(
            resource=ObjectRef("missing/type", "1"),
            action="read",
            actor=actor,
            context=None,
            using="default",
        ).reason
        == "unknown resource type: missing/type"
    )
    assert check(actor, action="missing").reason == "unknown action: blog/post#missing"


def test_enumeration_query_count_does_not_scale_with_candidates(active):
    actor = SubjectRef.of("auth/user", "alice")
    counts = []
    for size in (1, 30):
        IndexCover.objects.all().delete()
        with sudo(reason="reset index read fixture"):
            Post._base_manager.all().delete()
        Post._base_manager.bulk_create([Post(pk=i, title=str(i)) for i in range(1, size + 1)])
        for i in range(1, size + 1):
            cover(term("auth/user", str(i)), resource="1", condition=conditions.leaf("first", {}))
            cover(
                term("auth/user", "alice"), resource=str(i), condition=conditions.leaf("first", {})
            )
        with CaptureQueriesContext(connection) as accessible_queries:
            assert set(
                read.accessible_ids(
                    resource_type="blog/post",
                    action="read",
                    actor=actor,
                    context={"a": True},
                    using="default",
                )
            ) == {str(i) for i in range(1, size + 1)}
        with CaptureQueriesContext(connection) as subject_queries:
            result = read.lookup_subjects(
                resource=ObjectRef("blog/post", "1"),
                action="read",
                subject_type="auth/user",
                context={"a": True},
                using="default",
            )
        assert set(result) == {
            actor,
            *(SubjectRef.of("auth/user", str(i)) for i in range(1, size + 1)),
        }
        counts.append((len(accessible_queries), len(subject_queries)))
    assert counts[0] == counts[1]


@pytest.mark.pg_delta
@pytest.mark.parametrize("width", [1, 30])
def test_scope_sql_length_and_statement_count_independent_of_depth(active, width):
    actor = SubjectRef.of("auth/user", "alice")
    measurements = []
    for depth in (1, 50):
        # The source graph changes in depth and width; the scope still reads
        # the closure, with no per-hop SQL or Python frontier probes.
        index_harness.seed(
            [
                "blog/post:1#viewer@auth/user:alice",
                *(f"blog/post:{i}#parent@blog/post:{i - 1}" for i in range(2, depth + 1)),
                *(f"blog/post:{100 + i}#parent@blog/post:{depth}" for i in range(width)),
            ],
            backend=active,
        )
        with CaptureQueriesContext(connection) as queries:
            predicate = read.scope_q(Post, action="read", actor=actor, using="default")
            qs = Post._base_manager.filter(predicate).order_by()
            sql, _ = qs.query.get_compiler(using="default").as_sql()
            list(qs)
        measurements.append((len(sql), len(queries)))
        assert "EXISTS" in sql
        assert "rebac_grant" in sql
    assert measurements[0] == measurements[1]


def test_scope_width_does_not_change_sql(active):
    actor = SubjectRef.of("auth/user", "alice")
    measured = []
    for width in (1, 50):
        schema = (
            "definition auth/user {}\ndefinition blog/post {\n"
            + "\n".join(f"relation r{i}: auth/user" for i in range(width))
            + "\npermission read = "
            + " + ".join(f"r{i}" for i in range(width))
            + "\n}"
        )
        active.set_schema(parse_zed(schema))
        from tests.backend_setup import rebuild_backend

        rebuild_backend(active)
        with CaptureQueriesContext(connection) as queries:
            q = read.scope_q(Post, action="read", actor=actor, using="default")
            queryset = Post._base_manager.filter(q)
            sql, _ = queryset.query.get_compiler(using="default").as_sql()
            list(queryset)
        measured.append((len(sql), len(queries)))
    assert measured[0] == measured[1]


@pytest.mark.pg_delta
def test_embedded_querysets_remain_scoped(active):
    Folder._base_manager.bulk_create([Folder(pk=1, name="folder")])
    Post._base_manager.bulk_create(
        [
            Post(pk=1, title="allowed", folder_id=1),
            Post(pk=2, title="denied", folder_id=1),
        ]
    )
    actor = SubjectRef.of("auth/user", "alice")
    index_harness.seed(
        [
            "blog/post:1#viewer@auth/user:alice",
            "blog/folder:1#viewer@auth/user:alice",
        ],
        backend=active,
    )
    scoped = Post.objects.with_actor(actor).order_by("pk")
    assert list(
        Post._base_manager.filter(pk__in=scoped.values("pk")).values_list("pk", flat=True)
    ) == [1]
    assert list(
        Post._base_manager.filter(Exists(scoped.filter(pk=OuterRef("pk")))).values_list(
            "pk", flat=True
        )
    ) == [1]
    assert list(
        Folder._base_manager.annotate(first=Subquery(scoped.values("pk")[:1])).values_list(
            "first", flat=True
        )
    ) == [1]
    folders = list(
        Folder.objects.with_actor(actor).prefetch_related(
            Prefetch("posts", queryset=scoped, to_attr="visible_posts")
        )
    )
    assert [post.pk for post in folders[0].visible_posts] == [1]
    index_harness.assert_scope_matches(Post, actor=actor, action="read")


def test_embedding_without_actor_fails_closed(active):
    with pytest.raises(MissingActorError):
        list(Post._base_manager.filter(pk__in=Post.objects.all().values("pk")))


def test_lazy_scope_keeps_type_and_exception_arms_after_construction(active):
    Post._base_manager.bulk_create([Post(pk=1, title="one"), Post(pk=2, title="two")])
    term("blog/post", "2")
    actor = SubjectRef.of("auth/user", "alice")
    pending = Post._base_manager.filter(
        read.scope_q(Post, action="read", actor=actor, using="default")
    )
    alice = term("auth/user", "alice")
    broad = cover(alice, resource=None)
    add_ban(broad, alice, scope=term("blog/post", "1"))
    assert list(pending.values_list("pk", flat=True)) == [2]


def test_scope_rejects_noncanonical_wire_identity(active):
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    cover(term("auth/user", "alice"), resource="01")
    assert (
        list(
            Post._base_manager.filter(
                read.scope_q(
                    Post, action="read", actor=SubjectRef.of("auth/user", "alice"), using="default"
                )
            )
        )
        == []
    )


def test_manual_schema_change_refuses_every_read_until_rebuilt(active):
    from tests.backend_setup import rebuild_backend

    actor = SubjectRef.of("auth/user", "alice")
    index_harness.seed(["blog/post:1#viewer@auth/user:alice"], backend=active)
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    assert check(actor).allowed
    pending = Post._base_manager.filter(
        read.scope_q(Post, action="read", actor=actor, using="default")
    )
    active.set_schema(
        parse_zed(SCHEMA.replace("permission read = walk - blocked", "permission read = nil"))
    )
    assert list(pending) == []
    for operation in (
        lambda: check(actor),
        lambda: read.scope_q(Post, action="read", actor=actor, using="default"),
        lambda: active.accessible(subject=actor, resource_type="blog/post", action="read"),
        lambda: active.lookup_subjects(
            resource=ObjectRef("blog/post", "1"), action="read", subject_type="auth/user"
        ),
    ):
        with pytest.raises(SchemaError, match=r"rebac\.E013"):
            operation()
    rebuild_backend(active)
    assert not check(actor).allowed


def test_scope_plan_reuses_only_the_same_actor_and_program(active):
    from tests.backend_setup import rebuild_backend

    read._scope_cache.clear()
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    alice = SubjectRef.of("auth/user", "alice")
    bob = SubjectRef.of("auth/user", "bob")
    cover(term("auth/user", "alice"))

    def scoped(actor):
        queryset = Post._base_manager.filter(
            read.scope_q(Post, action="read", actor=actor, using="default")
        )
        queryset.query.sql_with_params()
        return queryset

    with patch.object(read, "member", wraps=read.member) as build:
        old = scoped(alice)
        _, alice_params = old.query.sql_with_params()
        assert list(old.values_list("pk", flat=True)) == [1]
        assert list(scoped(alice).values_list("pk", flat=True)) == [1]
        assert build.call_count == 1

        bob_scope = scoped(bob)
        _, bob_params = bob_scope.query.sql_with_params()
        assert "alice" in alice_params and "alice" not in bob_params
        assert "bob" in bob_params
        assert list(bob_scope) == []
        assert build.call_count == 1

        assert list(scoped(SubjectRef.of("auth/group", "staff", "member"))) == []
        assert build.call_count == 2

        active.set_schema(
            parse_zed(SCHEMA.replace("permission read = walk - blocked", "permission read = nil"))
        )
        # The already-compiled scope refreshes its readiness witness at use.
        assert list(old) == []
        rebuild_backend(active)
        assert list(scoped(alice)) == []
        assert build.call_count == 3


def test_empty_id_check_uses_sql_existence_not_python_resource_expansion(active, monkeypatch):
    actor = SubjectRef.of("auth/user", "alice")
    Post._base_manager.bulk_create([Post(pk=i, title=str(i)) for i in range(1, 1201)])
    cover(term("auth/user", "alice"), resource="1200", condition=conditions.leaf("first", {}))

    def enumeration_forbidden(*args, **kwargs):
        raise AssertionError("model-level check expanded resource IDs in Python")

    monkeypatch.setattr(read, "resource_ids", enumeration_forbidden)
    assert not check(actor, resource="", context={"a": False}).allowed
    assert check(actor, resource="", context={"a": True}).allowed


@pytest.mark.pg_delta
def test_context_accessible_uses_subqueries_for_more_than_sqlite_parameter_limit(active):
    actor = SubjectRef.of("auth/user", "alice")
    Post._base_manager.bulk_create([Post(pk=i, title=str(i)) for i in range(1, 1201)])
    intern([("blog/post", str(i), "") for i in range(1, 1201)], using="default")
    cover(term("auth/user", "alice"), resource=None, condition=conditions.leaf("first", {}))
    rows = read.accessible_ids(
        resource_type="blog/post", action="read", actor=actor, using="default", context={"a": True}
    )
    sql, params = rows.query.get_compiler(using="default").as_sql()
    assert "IN (SELECT" in sql
    assert len(params) < 999
    assert set(rows) == {str(i) for i in range(1, 1201)}


def test_subject_lookup_filters_in_sql_and_sorts_wire_references(active, monkeypatch):
    from django.db.models import QuerySet

    admitted = [
        SubjectRef.of("auth/user", value, relation)
        for value, relation in [("a", "member"), ("a!", ""), ("a", ""), ("Z", ""), ("é", "")]
    ]
    intern([("auth/user", f"unrelated-{i}", "") for i in range(1200)], using="default")
    for actor in admitted:
        cover(term(actor.subject_type, actor.subject_id, actor.optional_relation))
    original = QuerySet.iterator

    def guarded_iterator(rows, *args, **kwargs):
        if rows.model is IndexTerm:
            sql, _ = rows.query.get_compiler(using=rows.db).as_sql()
            assert "rebac_grant" in sql and "EXISTS" in sql
        return original(rows, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "iterator", guarded_iterator)
    assert read.lookup_subjects(
        resource=ObjectRef("blog/post", "1"),
        action="read",
        subject_type="auth/user",
        using="default",
    ) == sorted(admitted, key=str)


def test_execution_fence_rejects_index_mismatch_after_scope_construction(active):
    actor = SubjectRef.of("auth/user", "alice")
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    cover(term("auth/user", "alice"))
    pending = Post._base_manager.filter(
        read.scope_q(Post, action="read", actor=actor, using="default")
    )
    SchemaGeneration.objects.filter(pk=1).update(index_revision=None)
    assert list(pending) == []


def test_check_cannot_evaluate_new_index_with_old_caveat_schema(active, monkeypatch):
    from rebac.backends.local import LocalBackend

    actor = SubjectRef.of("auth/user", "alice")
    cover(term("auth/user", "alice"), condition=conditions.leaf("first", {}))
    database_backend = LocalBackend()
    monkeypatch.setattr(
        database_backend,
        "_load_schema_from_db",
        lambda using, overrides=(): (active.schema(), None),
    )
    original = read._plan_rows

    def publish_between_guard_and_read(*args, **kwargs):
        # Simulate atomic publication of a new schema/index after the initial
        # readiness SELECT. The existing conditional rows now mean something else.
        SchemaGeneration.objects.filter(pk=1).update(revision="9" * 32, index_revision="9" * 32)
        return original(*args, **kwargs)

    monkeypatch.setattr(read, "_plan_rows", publish_between_guard_and_read)
    assert not database_backend.check_access(
        subject=actor, resource=ObjectRef("blog/post", "1"), action="read", context={"a": True}
    ).allowed


@pytest.mark.parametrize(
    "context,expected",
    [(None, {"carol"}), ({"a": True}, {"carol"}), ({"a": False}, {"bob", "carol"})],
)
def test_subject_lookup_sql_keeps_conditional_membership_bans(active, context, expected):
    actors = {name: term("auth/user", name) for name in ("alice", "bob", "carol")}
    group = term("auth/group", "banned", "member")
    member(group, actors["alice"])
    member(group, actors["bob"], conditions.leaf("first", {}))
    # Enumeration lists the subjects a stored row names; a class holder such
    # as ``authenticated`` names nobody. Grant through a stored set.
    everyone = term("auth/group", "everyone", "member")
    for actor in actors.values():
        member(everyone, actor)
    grant = cover(everyone)
    add_ban(grant, group)
    result = read.lookup_subjects(
        resource=ObjectRef("blog/post", "1"),
        action="read",
        subject_type="auth/user",
        context=context,
        using="default",
    )
    assert result == sorted([SubjectRef.of("auth/user", name) for name in expected], key=str)
    for name in actors:
        assert check(SubjectRef.of("auth/user", name), context=context).allowed == (
            name in expected
        )


def test_context_enumeration_closes_when_pinned_schema_deadline_passes(active, monkeypatch):
    from rebac.backends.local import LocalBackend

    actor = SubjectRef.of("auth/user", "alice")
    deadline = index_now() + timedelta(hours=1)
    cover(term("auth/user", "alice"), condition=conditions.leaf("first", {}))
    database_backend = LocalBackend()
    monkeypatch.setattr(
        database_backend,
        "_load_schema_from_db",
        lambda using, overrides=(): (active.schema(), deadline),
    )
    with read.using_backend(database_backend):
        pending = read.accessible_ids(
            resource_type="blog/post",
            action="read",
            actor=actor,
            context={"a": True},
            using="default",
        )
    monkeypatch.setattr(index_time, "index_now", lambda: deadline)
    assert list(pending) == []


def test_new_exception_formula_after_context_preparation_remains_a_possible_ban(active):
    actor = SubjectRef.of("auth/user", "alice")
    alice = term("auth/user", "alice")
    grant = cover(alice, condition=conditions.leaf("first", {}))
    args = dict(
        resource_type="blog/post",
        action="read",
        actor=actor,
        context={"a": True, "b": False},
        using="default",
    )
    pending = read.accessible_ids(**args)
    add_ban(grant, alice, condition=conditions.leaf("second", {}))
    # This statement has not evaluated the newly introduced formula, so deny
    # conservatively; a fresh preparation can evaluate it as definitely false.
    assert list(pending) == []
    assert list(read.accessible_ids(**args)) == ["1"]


# Cases from the semantic review of the index design.


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_statement_compiled_before_an_override_does_not_run_against_the_new_program(
    settings, storage
):
    from django.contrib.contenttypes.models import ContentType

    from rebac.backends import reset_backend
    from rebac.models import SchemaDefinition, SchemaOverride, SchemaPermission, SchemaRelation
    from rebac.models.schema_write import schema_index_write

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    IndexState.objects.get_or_create(key="global")
    with schema_index_write("default"):
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="test/doc")
        for name in ("r1", "r2", "r3"):
            SchemaRelation.objects.create(
                definition=definition, name=name, allowed_subjects=[{"type": "auth/user"}]
            )
        permission = SchemaPermission.objects.create(
            definition=definition, name="read", expression="r1 & r2"
        )
    index_harness.seed([f"test/doc:one#{name}@auth/user:alice" for name in ("r1", "r2", "r3")])
    args = dict(
        resource_type="test/doc",
        action="read",
        actor=SubjectRef.of("auth/user", "alice"),
        using="default",
    )
    pending = read.accessible_ids(**args)
    assert list(read.accessible_ids(**args)) == ["one"]
    SchemaOverride.objects.create(
        kind="disable",
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=permission.pk,
        expression="r3",
        reason="ban",
    )
    assert list(read.accessible_ids(**args)) == []
    # The pending statement names the sites of the program before the
    # override. It must not be answered from the rows of the program after it.
    assert list(pending) == []


def test_statement_compiled_before_a_schema_change_does_not_run_after_the_rebuild(active):
    from rebac.index.rebuild import rebuild

    actor = SubjectRef.of("auth/user", "alice")
    index_harness.seed(
        ["blog/post:1#viewer@auth/user:alice", "blog/post:1#blocked@auth/user:alice"],
        backend=active,
    )
    rebuild(using="default")
    args = dict(resource_type="blog/post", action="public", actor=actor, using="default")
    pending = read.accessible_ids(**{**args, "action": "walk"})
    assert list(pending.all()) == ["1"]
    active.set_schema(
        parse_zed(
            SCHEMA.replace("permission walk = viewer + parent->walk", "permission walk = nil")
        )
    )
    rebuild(using="default")
    assert list(read.accessible_ids(**{**args, "action": "walk"})) == []
    assert list(pending) == []


def test_each_site_compiles_once_so_read_sql_is_linear_in_the_read_plan(settings):
    from rebac.backends import reset_backend
    from rebac.index.rebuild import rebuild

    reset_backend()
    IndexState.objects.get_or_create(key="global")
    active = backend()
    active.set_schema(
        parse_zed("""
            definition auth/user {}
            definition auth/anonymous {}
            definition test/doc {
                relation r1: auth/user
                relation r2: auth/user
                permission p1 = r1 - r2
                permission p2 = p1 & authenticated
                permission p3 = p2 - r2
                permission p4 = p3 & r1
            }
        """)
    )
    rebuild(using="default")
    index_harness.seed(
        [
            "test/doc:one#r1@auth/user:alice",
            "test/doc:two#r1@auth/user:alice",
            "test/doc:two#r2@auth/user:alice",
        ],
        backend=active,
    )
    program = program_for(active, using="default")
    actor = SubjectRef.of("auth/user", "alice")
    sizes = {}
    for name in ("r1", "p1", "p2", "p3", "p4"):
        key = ("test/doc", name)
        rows = IndexTerm.objects.filter(
            read.member(
                key,
                "pk",
                actor,
                True,
                program=program,
                using="default",
                now=read._ExecutionTime(),
            )
        )
        sql = str(rows.query)
        lookups = program.lookups(key)
        # One EXISTS per lookup of the plan: no site is compiled twice.
        assert sql.count("EXISTS") == lookups, name
        sizes[lookups] = len(sql)
        expected = ["one", "two"] if name == "r1" else ["one"]
        assert sorted(rows.values_list("object_id", flat=True)) == expected, name
    assert sorted(sizes) == [1, 3, 5, 7, 9]
    # a + b*k. An object nested under d sites is one CASE per site, so the
    # cost of a lookup grows with its depth; allow a quarter over the first.
    fixed = sizes[1]
    per_lookup = 1.25 * (sizes[3] - sizes[1]) / 2
    for lookups, size in sizes.items():
        assert size <= fixed + per_lookup * (lookups - 1), (lookups, size)


@pytest.mark.parametrize("instances", [20, pytest.param(200, marks=pytest.mark.slow)])
def test_context_enumeration_sql_does_not_grow_with_the_number_of_caveat_instances(
    settings, instances
):
    from rebac.backends import reset_backend
    from rebac.index.rebuild import rebuild

    reset_backend()
    IndexState.objects.get_or_create(key="global")
    active = backend()
    active.set_schema(
        parse_zed("""
            caveat above(a bool, b int) { a && b > 0 }
            definition auth/user {}
            definition test/doc {
                relation r1: auth/user with above
                permission read = r1
            }
        """)
    )
    rebuild(using="default")
    actor = SubjectRef.of("auth/user", "alice")
    measured = []
    for size in (1, instances):
        active.write_relationships(
            [
                RelationshipTuple(ObjectRef("test/doc", str(b)), "r1", actor, "above", {"b": b})
                for b in range(-1, size)
            ]
        )
        rows = read.accessible_ids(
            resource_type="test/doc",
            action="read",
            actor=actor,
            using="default",
            context={"a": True},
        )
        sql, params = rows.query.get_compiler(using="default").as_sql()
        assert set(rows) == {str(b) for b in range(1, size)}
        assert not read.check(
            resource=ObjectRef("test/doc", "0"),
            action="read",
            actor=actor,
            context={"a": True},
            using="default",
        ).allowed
        measured.append((len(sql), len(params)))
    assert measured[0] == measured[1]


@pytest.mark.pg_delta
def test_scope_plan_parameters_are_prepared_for_the_vendor(active, monkeypatch):
    from datetime import UTC, datetime

    read._scope_cache.clear()
    instant = datetime(2031, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
    monkeypatch.setattr(index_time, "index_now", lambda: instant)
    actor = SubjectRef.of("auth/user", "alice")
    for _ in range(2):  # a cold plan, then the cached one
        queryset = Post._base_manager.filter(
            read.scope_q(Post, action="read", actor=actor, using="default")
        )
        _sql, params = queryset.query.sql_with_params()
        assert connection.ops.adapt_datetimefield_value(instant) in params
        assert instant not in params or connection.vendor == "postgresql"
