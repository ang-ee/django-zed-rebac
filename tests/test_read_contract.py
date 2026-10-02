"""Read semantics of compiled permissions.

Every case writes source facts (tuples and model rows) and reads them back
through the backend, a scoped queryset or ``rebac.compile.read``; harness cases
also compare the result with the frozen walker and the reference model.
"""

from datetime import timedelta

import pytest
from django.db import connection
from django.db.models import Exists, OuterRef, Prefetch, Subquery
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from rebac import ObjectRef, RelationshipTuple, SubjectRef, backend, sudo
from rebac.actors import anonymous_actor
from rebac.clock import application_now
from rebac.compile import read
from rebac.errors import MissingActorError
from rebac.evaluator import evaluator_scope
from rebac.models.generation import SchemaGeneration
from rebac.schema import parse_zed
from rebac.types import PermissionResult, RelationshipFilter
from tests import reference_harness
from tests.backend_setup import install_schema
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db

ALICE = SubjectRef.of("auth/user", "alice")
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


EXPIRING_SCHEMA = """
definition auth/user {}
definition auth/group {
    relation member: auth/user | auth/user with expiration
}
definition blog/post {
    relation viewer: auth/user | auth/group#member | auth/user with expiration
    relation blocked: auth/user | auth/user with expiration
    permission read = viewer - blocked
}
"""


@pytest.fixture(params=("denormalized", "registry"))
def storage(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        yield request.param


@pytest.fixture
def active(storage):
    current = backend()
    install_schema(current, parse_zed(SCHEMA))
    return current


@pytest.fixture
def expiring(storage):
    current = backend()
    install_schema(current, parse_zed(EXPIRING_SCHEMA))
    return current


def row(resource, relation, subject, **payload):
    return RelationshipTuple(
        ObjectRef.parse(resource), relation, SubjectRef.parse(subject), **payload
    )


def check(actor, resource="1", **kwargs):
    return read.check(
        backend=backend(),
        resource=ObjectRef("blog/post", resource),
        actor=actor,
        action=kwargs.pop("action", "read"),
        context=kwargs.pop("context", None),
        using="default",
        **kwargs,
    )


def scope(actor, action="read"):
    return Post._base_manager.filter(
        read.scope_q(backend=backend(), model=Post, action=action, actor=actor, using="default")
    )


def test_all_conditional_membership_paths_and_missing_sets(active):
    active.write_relationships(
        [
            row("auth/group:staff", "member", "auth/user:alice", caveat_name="first"),
            row("auth/group:staff", "member", "auth/user:alice", caveat_name="second"),
            row("blog/post:1", "viewer", "auth/group:staff#member"),
        ]
    )
    result = check(ALICE)
    assert result.result is PermissionResult.CONDITIONAL_PERMISSION
    assert result.conditional_on == ("a", "b")
    assert check(ALICE, context={"a": False}).conditional_on == ("b",)
    assert check(ALICE, context={"a": False, "b": True}).allowed
    assert check(ALICE, context={"a": False, "b": False}).result is PermissionResult.NO_PERMISSION


def test_conditional_exclusion_memberships_are_possible_denials(active):
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    active.write_relationships(
        [
            row("auth/group:blocked", "member", "auth/user:alice", caveat_name="first"),
            row("auth/group:blocked", "member", "auth/user:alice", caveat_name="second"),
            row("blog/post:1", "blocked", "auth/group:blocked#member"),
        ]
    )
    assert check(ALICE, action="public").conditional_on == ("a", "b")
    assert check(ALICE, action="public", context={"a": False, "b": False}).allowed
    assert not check(ALICE, action="public", context={"a": False, "b": True}).allowed
    assert list(scope(ALICE, "public")) == []


def test_type_level_grant_concrete_exclusion_and_empty_resource(active):
    Post._base_manager.bulk_create([Post(pk=1, title="one"), Post(pk=2, title="two")])
    reference_harness.seed(["blog/post:1#blocked@auth/user:alice"], backend=active)
    assert not check(ALICE, "1", action="public").allowed
    assert check(ALICE, "2", action="public").allowed
    assert check(ALICE, "", action="public").allowed
    assert set(
        read.accessible_ids(
            backend=active, resource_type="blog/post", action="public", actor=ALICE, using="default"
        )
    ) == {"2"}
    assert not check(ALICE, "", action="walk").allowed
    reference_harness.seed(["blog/post:1#viewer@auth/user:alice"], backend=active)
    assert check(ALICE, "", action="walk").allowed


@pytest.mark.pg_delta
def test_intervals_are_half_open(expiring, monkeypatch):
    now = application_now()
    hour, day = now + timedelta(hours=1), now + timedelta(days=1)
    monkeypatch.setattr(timezone, "now", lambda: now)
    expiring.write_relationships([row("blog/post:1", "viewer", "auth/user:alice", expires_at=day)])
    assert check(ALICE).allowed
    expiring.write_relationships([row("blog/post:1", "blocked", "auth/user:alice", expires_at=now)])
    assert check(ALICE).allowed
    expiring.write_relationships(
        [row("blog/post:1", "blocked", "auth/user:alice", expires_at=hour)]
    )
    assert not check(ALICE).allowed
    now = hour
    assert check(ALICE).allowed
    now = day
    assert not check(ALICE).allowed


def test_model_less_ids_and_context_enumeration(active):
    reference_harness.seed(["virtual/item:external#viewer@auth/user:alice"], backend=active)
    assert set(active.accessible(subject=ALICE, action="read", resource_type="virtual/item")) == {
        "external"
    }
    active.write_relationships(
        [row("blog/post:1", "viewer", "auth/user:alice", caveat_name="first")]
    )
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    assert list(active.accessible(subject=ALICE, action="read", resource_type="blog/post")) == []
    assert list(
        active.accessible(
            subject=ALICE, action="read", resource_type="blog/post", context={"a": True}
        )
    ) == ["1"]


def test_lookup_subjects_expands_nested_members_and_excludes_denials(active):
    reference_harness.seed(
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
    ) == {ALICE}
    reference_harness.assert_reads_match(
        subjects=[SubjectRef.of("auth/user", name) for name in ("alice", "bob", "outsider")],
        resources=[resource],
        actions=["read"],
    )


def test_lookup_subjects_context(active):
    active.write_relationships(
        [row("blog/post:1", "viewer", "auth/user:alice", caveat_name="first")]
    )
    args = dict(resource=ObjectRef("blog/post", "1"), action="read", subject_type="auth/user")
    assert list(active.lookup_subjects(**args)) == []
    assert list(active.lookup_subjects(**args, context={"a": True})) == [ALICE]


def test_harness_conditional_paths_and_wildcard_groups(active):
    active.write_relationships(
        [
            RelationshipTuple(
                ObjectRef("auth/group", "conditional"),
                "member",
                ALICE,
                caveat_name=name,
            )
            for name in ("first", "second")
        ]
    )
    reference_harness.seed(
        [
            "auth/group:everyone#member@auth/user:*",
            "blog/post:conditional#viewer@auth/group:conditional#member",
            "blog/post:public#viewer@auth/group:everyone#member",
            "blog/post:public#blocked@auth/group:conditional#member",
        ],
        backend=active,
    )
    reference_harness.assert_reads_match(
        subjects=[
            ALICE,
            SubjectRef.of("auth/user", "unknown"),
            SubjectRef.of("auth/group", "conditional", "member"),
            anonymous_actor(),
        ],
        resources=[ObjectRef("blog/post", "conditional"), ObjectRef("blog/post", "public")],
        actions=["read"],
        contexts=[None, {"a": False}, {"a": False, "b": True}, {"a": False, "b": False}],
    )


def test_missing_policy_row_fails_closed(active):
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    reference_harness.seed(["blog/post:1#viewer@auth/user:alice"], backend=active)
    resource = ObjectRef("blog/post", "1")
    assert check(ALICE).allowed
    SchemaGeneration.objects.filter(pk=1).delete()
    assert check(ALICE).result is PermissionResult.NO_PERMISSION
    assert list(scope(ALICE)) == []
    assert list(active.accessible(subject=ALICE, action="read", resource_type="blog/post")) == []
    assert (
        list(active.lookup_subjects(resource=resource, action="read", subject_type="auth/user"))
        == []
    )


@pytest.mark.pg_delta
def test_identity_filter_rejects_noncanonical_integer_spelling(active):
    from rebac._id import model_identity_filter

    post = Post(pk=1, title="canonical")
    Post._base_manager.bulk_create([post])
    assert Post._base_manager.filter(model_identity_filter(Post, "pk", "1")).get() == post
    for wire in ("01", "+1", " 1", "1\n", "\u0661", "*", "", str(2**100)):
        assert not Post._base_manager.filter(model_identity_filter(Post, "pk", wire)).exists()


@pytest.mark.django_db(transaction=True)
def test_decision_cache_observes_owned_revoke(active):
    resource = ObjectRef("blog/post", "1")
    active.write_relationships([RelationshipTuple(resource, "viewer", ALICE)])
    with evaluator_scope() as evaluator:
        assert evaluator.check(active, subject=ALICE, action="read", resource=resource).allowed
        active.delete_relationships(
            RelationshipFilter(resource_type="blog/post", resource_id="1", relation="viewer")
        )
        assert not evaluator.check(active, subject=ALICE, action="read", resource=resource).allowed


@pytest.mark.parametrize("expired_part", ["grant", "membership", "exclusion"])
def test_reused_scoped_queryset_binds_time_at_execution(expiring, monkeypatch, expired_part):
    instant = application_now()
    deadline = instant + timedelta(hours=1)
    monkeypatch.setattr(timezone, "now", lambda: instant)
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    if expired_part == "membership":
        expiring.write_relationships(
            [
                row("auth/group:g", "member", "auth/user:alice", expires_at=deadline),
                row("blog/post:1", "viewer", "auth/group:g#member"),
            ]
        )
    elif expired_part == "exclusion":
        expiring.write_relationships(
            [
                row("blog/post:1", "viewer", "auth/user:alice"),
                row("blog/post:1", "blocked", "auth/user:alice", expires_at=deadline),
            ]
        )
    else:
        expiring.write_relationships(
            [row("blog/post:1", "viewer", "auth/user:alice", expires_at=deadline)]
        )
    scoped = Post.objects.with_actor(ALICE).scoped()
    assert list(scoped.values_list("pk", flat=True)) == ([] if expired_part == "exclusion" else [1])
    instant = deadline
    assert list(scoped.values_list("pk", flat=True)) == ([1] if expired_part == "exclusion" else [])


def test_unknown_type_and_action_preserve_reason(active):
    assert (
        read.check(
            backend=active,
            resource=ObjectRef("missing/type", "1"),
            action="read",
            actor=ALICE,
            context=None,
            using="default",
        ).reason
        == "unknown resource type: missing/type"
    )
    assert check(ALICE, action="missing").reason == "unknown action: blog/post#missing"


def conditional_candidates(active, size):
    """``size`` posts alice may read and ``size`` users who may read post 1, all under ``first``."""
    active.delete_relationships(RelationshipFilter(resource_type="blog/post"))
    with sudo(reason="reset read fixture"):
        Post._base_manager.all().delete()
    Post._base_manager.bulk_create([Post(pk=i, title=str(i)) for i in range(1, size + 1)])
    active.write_relationships(
        [
            tuple_
            for i in range(1, size + 1)
            for tuple_ in (
                row("blog/post:1", "viewer", f"auth/user:{i}", caveat_name="first"),
                row(f"blog/post:{i}", "viewer", "auth/user:alice", caveat_name="first"),
            )
        ]
    )


def test_enumeration_query_count_does_not_scale_with_candidates(active):
    counts = []
    for size in (1, 30):
        conditional_candidates(active, size)
        with CaptureQueriesContext(connection) as queries:
            assert set(
                read.accessible_ids(
                    backend=active,
                    resource_type="blog/post",
                    action="read",
                    actor=ALICE,
                    context={"a": True},
                    using="default",
                )
            ) == {str(i) for i in range(1, size + 1)}
        counts.append(len(queries))
    assert counts[0] == counts[1]


def test_subject_lookup_costs_a_bounded_number_of_statements_per_candidate(active):
    counts = []
    for size in (1, 30):
        conditional_candidates(active, size)
        with CaptureQueriesContext(connection) as queries:
            result = read.lookup_subjects(
                backend=active,
                resource=ObjectRef("blog/post", "1"),
                action="read",
                subject_type="auth/user",
                context={"a": True},
                using="default",
            )
        assert set(result) == {
            ALICE,
            *(SubjectRef.of("auth/user", str(i)) for i in range(1, size + 1)),
        }
        counts.append(len(queries))
    # Each candidate is one point check: its sets, then the lower bound.
    assert counts[1] - counts[0] <= 4 * (30 - 1)


@pytest.mark.pg_delta
@pytest.mark.parametrize("width", [1, 30])
def test_scope_sql_length_and_statement_count_independent_of_depth(active, width):
    measurements = []
    for depth in (1, 50):
        # The source graph changes in depth and width; the scope is one
        # statement of the same text, with no per-hop SQL or Python probes.
        reference_harness.seed(
            [
                "blog/post:1#viewer@auth/user:alice",
                *(f"blog/post:{i}#parent@blog/post:{i - 1}" for i in range(2, depth + 1)),
                *(f"blog/post:{100 + i}#parent@blog/post:{depth}" for i in range(width)),
            ],
            backend=active,
        )
        with CaptureQueriesContext(connection) as queries:
            qs = scope(ALICE).order_by()
            sql, _ = qs.query.get_compiler(using="default").as_sql()
            list(qs)
        measurements.append((len(sql), len(queries)))
        assert "EXISTS" in sql
    assert measurements[0] == measurements[1]


def test_scope_width_does_not_change_statement_count(active):
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
        with CaptureQueriesContext(connection) as queries:
            queryset = scope(ALICE)
            queryset.query.get_compiler(using="default").as_sql()
            list(queryset)
        measured.append(len(queries))
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
    reference_harness.seed(
        [
            "blog/post:1#viewer@auth/user:alice",
            "blog/folder:1#viewer@auth/user:alice",
        ],
        backend=active,
    )
    scoped = Post.objects.with_actor(ALICE).order_by("pk")
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
        Folder.objects.with_actor(ALICE).prefetch_related(
            Prefetch("posts", queryset=scoped, to_attr="visible_posts")
        )
    )
    assert [post.pk for post in folders[0].visible_posts] == [1]
    reference_harness.assert_scope_matches(Post, actor=ALICE, action="read")


def test_embedding_without_actor_fails_closed(active):
    with pytest.raises(MissingActorError):
        list(Post._base_manager.filter(pk__in=Post.objects.all().values("pk")))


def test_lazy_scope_keeps_type_and_exclusion_arms_after_construction(active):
    Post._base_manager.bulk_create([Post(pk=1, title="one"), Post(pk=2, title="two")])
    pending = scope(ALICE, "public").order_by("pk")
    reference_harness.seed(["blog/post:1#blocked@auth/user:alice"], backend=active)
    assert list(pending.values_list("pk", flat=True)) == [2]


def test_scope_rejects_noncanonical_wire_identity(active):
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    reference_harness.seed(["blog/post:01#viewer@auth/user:alice"], backend=active)
    assert list(scope(ALICE)) == []
    assert not check(ALICE, "1").allowed


def test_manual_schema_change_closes_pending_scope_and_applies_to_every_read(active):
    reference_harness.seed(["blog/post:1#viewer@auth/user:alice"], backend=active)
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    assert check(ALICE).allowed
    pending = scope(ALICE)
    assert list(pending.values_list("pk", flat=True)) == [1]
    active.set_schema(
        parse_zed(SCHEMA.replace("permission read = walk - blocked", "permission read = nil"))
    )
    assert list(pending) == []
    assert check(ALICE).result is PermissionResult.NO_PERMISSION
    assert list(scope(ALICE)) == []
    assert list(active.accessible(subject=ALICE, resource_type="blog/post", action="read")) == []
    assert (
        list(
            active.lookup_subjects(
                resource=ObjectRef("blog/post", "1"), action="read", subject_type="auth/user"
            )
        )
        == []
    )


def test_kept_scope_statement_binds_each_actor_and_follows_the_policy(active):
    read.reset()
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    bob = SubjectRef.of("auth/user", "bob")
    reference_harness.seed(["blog/post:1#viewer@auth/user:alice"], backend=active)

    old = scope(ALICE)
    alice_sql, alice_params = old.query.sql_with_params()
    assert list(old.values_list("pk", flat=True)) == [1]
    assert list(scope(ALICE).values_list("pk", flat=True)) == [1]

    bob_scope = scope(bob)
    bob_sql, bob_params = bob_scope.query.sql_with_params()
    assert bob_sql == alice_sql
    assert "alice" in alice_params and "alice" not in bob_params
    assert "bob" in bob_params
    assert list(bob_scope) == []

    assert list(scope(SubjectRef.of("auth/group", "staff", "member"))) == []

    active.set_schema(
        parse_zed(SCHEMA.replace("permission read = walk - blocked", "permission read = nil"))
    )
    # The already-compiled scope checks its policy revision at use.
    assert list(old) == []
    assert list(scope(ALICE)) == []


def test_empty_id_check_uses_sql_existence_not_python_resource_expansion(active, monkeypatch):
    Post._base_manager.bulk_create([Post(pk=i, title=str(i)) for i in range(1, 1201)])
    active.write_relationships(
        [row("blog/post:1200", "viewer", "auth/user:alice", caveat_name="first")]
    )

    def enumeration_forbidden(*args, **kwargs):
        raise AssertionError("model-level check expanded resource IDs in Python")

    monkeypatch.setattr(read._AccessibleResources, "__iter__", enumeration_forbidden)
    assert not check(ALICE, resource="", context={"a": False}).allowed
    assert check(ALICE, resource="", context={"a": True}).allowed


@pytest.mark.pg_delta
def test_context_accessible_enumerates_more_ids_than_the_sqlite_parameter_limit(active):
    Post._base_manager.bulk_create([Post(pk=i, title=str(i)) for i in range(1, 1201)])
    active.write_relationships(
        [
            row(f"blog/post:{i}", "viewer", "auth/user:alice", caveat_name="first")
            for i in range(1, 1201)
        ]
    )
    if connection.vendor == "postgresql":
        # A bulk load inside the test's transaction leaves the planner without
        # statistics, and it then nests loops over the whole load.
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE")
    args = dict(
        backend=active, resource_type="blog/post", action="read", actor=ALICE, using="default"
    )
    assert set(read.accessible_ids(**args, context={"a": True})) == {str(i) for i in range(1, 1201)}
    assert set(read.accessible_ids(**args, context={"a": False})) == set()


def test_subject_lookup_sorts_wire_references(active):
    admitted = [SubjectRef.of("auth/user", value) for value in ("a!", "a", "Z", "é")]
    active.write_relationships(
        [
            *(
                RelationshipTuple(ObjectRef("blog/post", "1"), "viewer", actor)
                for actor in admitted
            ),
            *(row("blog/post:other", "viewer", f"auth/user:unrelated-{i}") for i in range(1200)),
        ]
    )
    assert read.lookup_subjects(
        backend=active,
        resource=ObjectRef("blog/post", "1"),
        action="read",
        subject_type="auth/user",
        using="default",
    ) == sorted(admitted, key=str)


def test_execution_fence_closes_scope_when_policy_row_is_removed_after_construction(active):
    Post._base_manager.bulk_create([Post(pk=1, title="one")])
    reference_harness.seed(["blog/post:1#viewer@auth/user:alice"], backend=active)
    pending = scope(ALICE)
    assert list(pending.values_list("pk", flat=True)) == [1]
    SchemaGeneration.objects.filter(pk=1).delete()
    assert list(pending) == []


@pytest.mark.parametrize(
    "context,expected",
    [(None, {"carol"}), ({"a": True}, {"carol"}), ({"a": False}, {"bob", "carol"})],
)
def test_subject_lookup_keeps_conditional_membership_bans(active, context, expected):
    names = ("alice", "bob", "carol")
    active.write_relationships(
        [
            row("auth/group:banned", "member", "auth/user:alice"),
            row("auth/group:banned", "member", "auth/user:bob", caveat_name="first"),
            # Enumeration lists the subjects a stored row names; a class holder
            # such as ``authenticated`` names nobody. Grant through a stored set.
            *(row("auth/group:everyone", "member", f"auth/user:{name}") for name in names),
            row("blog/post:1", "viewer", "auth/group:everyone#member"),
            row("blog/post:1", "blocked", "auth/group:banned#member"),
        ]
    )
    result = read.lookup_subjects(
        backend=active,
        resource=ObjectRef("blog/post", "1"),
        action="read",
        subject_type="auth/user",
        context=context,
        using="default",
    )
    assert result == sorted([SubjectRef.of("auth/user", name) for name in expected], key=str)
    for name in names:
        assert check(SubjectRef.of("auth/user", name), context=context).allowed == (
            name in expected
        )


def test_ban_written_after_context_enumeration_was_requested_applies_when_it_is_read(active):
    active.write_relationships(
        [row("blog/post:1", "viewer", "auth/user:alice", caveat_name="first")]
    )
    args = dict(
        backend=active, resource_type="blog/post", action="read", actor=ALICE, using="default"
    )
    pending = read.accessible_ids(**args, context={"a": True, "b": True})
    active.write_relationships(
        [
            row("auth/group:banned", "member", "auth/user:alice", caveat_name="second"),
            row("blog/post:1", "blocked", "auth/group:banned#member"),
        ]
    )
    assert list(pending) == []
    assert list(read.accessible_ids(**args, context={"a": True})) == []
    assert list(read.accessible_ids(**args, context={"a": True, "b": False})) == ["1"]


# Cases from the semantic review of the read design.


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_statement_compiled_before_an_override_does_not_run_against_the_new_policy(
    settings, storage
):
    from django.contrib.contenttypes.models import ContentType

    from rebac import schema_changes
    from rebac.backends import reset_backend
    from rebac.models import SchemaDefinition, SchemaOverride, SchemaPermission, SchemaRelation

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    with schema_changes():
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="test/doc")
        for name in ("r1", "r2", "r3"):
            SchemaRelation.objects.create(
                definition=definition, name=name, allowed_subjects=[{"type": "auth/user"}]
            )
        permission = SchemaPermission.objects.create(
            definition=definition, name="read", expression="r1 & r2"
        )
    reference_harness.seed([f"test/doc:one#{name}@auth/user:alice" for name in ("r1", "r2", "r3")])
    args = dict(resource_type="test/doc", action="read", actor=ALICE, using="default")
    pending = read.accessible_ids(backend=backend(), **args)
    assert list(read.accessible_ids(backend=backend(), **args)) == ["one"]
    SchemaOverride.objects.create(
        kind="disable",
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=permission.pk,
        expression="r3",
        reason="ban",
    )
    assert list(read.accessible_ids(backend=backend(), **args)) == []
    # The pending request was made under the policy before the override. It
    # must not be answered as if that policy still held.
    assert list(pending) == []


def test_statement_compiled_before_a_schema_change_does_not_run_after_it(active):
    reference_harness.seed(
        ["blog/post:1#viewer@auth/user:alice", "blog/post:1#blocked@auth/user:alice"],
        backend=active,
    )
    args = dict(
        backend=active, resource_type="blog/post", action="walk", actor=ALICE, using="default"
    )
    pending = read.accessible_ids(**args)
    assert list(pending) == ["1"]
    active.set_schema(
        parse_zed(
            SCHEMA.replace("permission walk = viewer + parent->walk", "permission walk = nil")
        )
    )
    assert list(read.accessible_ids(**args)) == []
    assert list(pending) == []


def test_nested_exclusion_and_intersection_chain_reads_each_level():
    active = install_schema_text("""
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
    reference_harness.seed(
        [
            "test/doc:one#r1@auth/user:alice",
            "test/doc:two#r1@auth/user:alice",
            "test/doc:two#r2@auth/user:alice",
        ],
        backend=active,
    )
    for name in ("r1", "p1", "p2", "p3", "p4"):
        rows = read.accessible_ids(
            backend=active, resource_type="test/doc", action=name, actor=ALICE, using="default"
        )
        assert sorted(rows) == (["one", "two"] if name == "r1" else ["one"]), name
    reference_harness.assert_reads_match(
        subjects=[ALICE, SubjectRef.of("auth/user", "bob")],
        resources=[ObjectRef("test/doc", "one"), ObjectRef("test/doc", "two")],
        actions=["r1", "p1", "p2", "p3", "p4"],
    )


def install_schema_text(text):
    from rebac.testing import install_schema as install

    return install(text)


@pytest.mark.parametrize("instances", [20, pytest.param(200, marks=pytest.mark.slow)])
def test_context_enumeration_statements_do_not_grow_with_the_number_of_caveat_instances(
    instances,
):
    active = install_schema_text("""
        caveat above(a bool, b int) { a && b > 0 }
        definition auth/user {}
        definition test/doc {
            relation r1: auth/user with above
            permission read = r1
        }
    """)
    measured = []
    for size in (1, instances):
        active.write_relationships(
            [
                RelationshipTuple(ObjectRef("test/doc", str(b)), "r1", ALICE, "above", {"b": b})
                for b in range(-1, size)
            ]
        )
        with CaptureQueriesContext(connection) as queries:
            rows = set(
                read.accessible_ids(
                    backend=active,
                    resource_type="test/doc",
                    action="read",
                    actor=ALICE,
                    using="default",
                    context={"a": True},
                )
            )
        assert rows == {str(b) for b in range(1, size)}
        assert not read.check(
            backend=active,
            resource=ObjectRef("test/doc", "0"),
            action="read",
            actor=ALICE,
            context={"a": True},
            using="default",
        ).allowed
        measured.append(len(queries))
    assert measured[0] == measured[1]


@pytest.mark.pg_delta
def test_scope_statement_parameters_are_prepared_for_the_vendor(expiring, monkeypatch):
    from datetime import UTC, datetime

    read.reset()
    instant = datetime(2031, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: instant)
    for _ in range(2):  # a cold statement, then the kept one
        _sql, params = scope(ALICE).query.sql_with_params()
        assert connection.ops.adapt_datetimefield_value(instant) in params
        assert instant not in params or connection.vendor == "postgresql"
