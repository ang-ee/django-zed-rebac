"""Queryset visibility agrees with graph evaluation for shared permission paths."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Count, Sum
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from rebac import (
    LocalBackend,
    ObjectRef,
    PermissionDepthExceeded,
    PermissionResult,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    backend,
    evaluator_scope,
    sudo,
    to_object_ref,
    to_subject_ref,
)
from rebac.backends import reset_backend
from rebac.models import active_relationship_model
from rebac.schema import parse_zed
from tests.testapp.models import AuthoredPost, Folder, Post

pytestmark = pytest.mark.django_db(transaction=True)

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")
EDITORS = SubjectRef.of("auth/group", "editors", "member")
TEAM = SubjectRef.of("work/team", "editorial", "member")
SCHEMA = """
definition auth/user {}
definition auth/group {
    relation member: auth/user
}
definition work/team {
    relation member: auth/user | auth/group#member
}
definition blog/folder {
    relation viewer: auth/user | auth/group#member
    permission read = viewer
}
definition blog/post {
    relation owner: auth/user
    relation shared: auth/user | auth/group#member | work/team#member
    relation approved: auth/user
    relation blocked: auth/user | auth/group#member
    relation folder: blog/folder // rebac:field=folder
    permission union_read = owner + shared
    permission intersection_read = union_read & approved
    permission read = intersection_read - blocked
    permission inherited_read = (owner + folder->read) - blocked
}
definition blog/authoredpost {
    relation owner: auth/user // rebac:field=author
    relation shared: auth/user | auth/group#member
    relation blocked: auth/user
    permission read = (owner + shared) - blocked
}
"""


@pytest.fixture(params=["denormalized", "registry"])
def active(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        local = backend()
        assert isinstance(local, LocalBackend)
        local.set_schema(parse_zed(SCHEMA))
        try:
            yield local
        finally:
            reset_backend()


def _posts(*titles):
    with sudo(reason="queryset permission parity fixtures"):
        return {title: Post.objects.create(title=title) for title in titles}


def _grant(row, relation, subject):
    return RelationshipTuple(to_object_ref(row), relation, subject)


def _assert_post_visibility(active, queryset, actor, action, expected):
    """Assert an explicit answer as well as parity with independent graph lookup."""
    expected_ids = {post.pk for post in expected}
    graph_ids = set(active.accessible(subject=actor, action=action, resource_type="blog/post"))
    assert graph_ids == {str(pk) for pk in expected_ids}
    row_ids = list(queryset.values_list("pk", flat=True))
    assert set(row_ids) == expected_ids
    assert len(row_ids) == len(expected_ids)
    assert queryset.count() == len(expected_ids)
    assert queryset.aggregate(count=Count("pk"), total=Sum("pk")) == {
        "count": len(expected_ids),
        "total": sum(expected_ids) if expected_ids else None,
    }
    projected = queryset.scoped_for_aggregate().values("pk")
    assert set(Post._base_manager.filter(pk__in=projected).values_list("pk", flat=True)) == (
        expected_ids
    )


@pytest.fixture
def shared_posts(active):
    posts = _posts("owned", "shared", "both", "unapproved", "blocked", "other", "hidden")
    active.write_relationships(
        [
            _grant(posts["owned"], "owner", ALICE),
            _grant(posts["shared"], "owner", BOB),
            _grant(posts["shared"], "shared", ALICE),
            _grant(posts["both"], "owner", ALICE),
            _grant(posts["both"], "shared", ALICE),
            _grant(posts["unapproved"], "shared", ALICE),
            _grant(posts["blocked"], "shared", ALICE),
            _grant(posts["blocked"], "blocked", ALICE),
            _grant(posts["other"], "owner", BOB),
            _grant(posts["other"], "approved", BOB),
            *[
                _grant(posts[title], "approved", ALICE)
                for title in ("owned", "shared", "both", "blocked")
            ],
        ]
    )
    return posts


@pytest.mark.parametrize(
    ("action", "visible"),
    [
        ("union_read", ("owned", "shared", "both", "unapproved", "blocked")),
        ("intersection_read", ("owned", "shared", "both", "blocked")),
        ("read", ("owned", "shared", "both")),
    ],
)
def test_shared_paths_and_boolean_permissions_preserve_rows_and_counts(
    active, shared_posts, action, visible
):
    queryset = Post.objects.with_actor(ALICE).with_action(action)
    _assert_post_visibility(
        active, queryset, ALICE, action, [shared_posts[name] for name in visible]
    )
    assert not queryset.filter(pk=shared_posts["other"].pk).exists()
    assert not queryset.filter(pk=shared_posts["hidden"].pk).exists()


def test_subject_sets_expand_nested_groups_and_exclusions(active):
    posts = _posts("direct", "group", "nested", "overlap", "blocked", "outsider")
    blockers = SubjectRef.of("auth/group", "blocked-editors", "member")
    active.write_relationships(
        [
            RelationshipTuple(EDITORS.object, "member", ALICE),
            RelationshipTuple(TEAM.object, "member", EDITORS),
            RelationshipTuple(blockers.object, "member", ALICE),
            _grant(posts["direct"], "shared", ALICE),
            _grant(posts["group"], "shared", EDITORS),
            _grant(posts["nested"], "shared", TEAM),
            _grant(posts["overlap"], "shared", ALICE),
            _grant(posts["overlap"], "shared", EDITORS),
            _grant(posts["overlap"], "shared", TEAM),
            _grant(posts["blocked"], "shared", TEAM),
            _grant(posts["blocked"], "blocked", blockers),
            _grant(posts["outsider"], "shared", BOB),
            *[_grant(post, "approved", ALICE) for post in posts.values()],
        ]
    )
    _assert_post_visibility(
        active,
        Post.objects.with_actor(ALICE),
        ALICE,
        "read",
        [posts[name] for name in ("direct", "group", "nested", "overlap")],
    )
    assert not Post.objects.with_actor(BOB).exists()


def test_field_backed_owner_keeps_direct_and_group_shares(active):
    alice = get_user_model().objects.create_user(username="author-alice")
    bob = get_user_model().objects.create_user(username="author-bob")
    alice_ref = to_subject_ref(alice)
    with sudo(reason="field owner and sharing fixtures"):
        owned = AuthoredPost.objects.create(title="owned", author=alice)
        shared = AuthoredPost.objects.create(title="shared", author=bob)
        group = AuthoredPost.objects.create(title="group", author=bob)
        blocked = AuthoredPost.objects.create(title="blocked", author=alice)
        hidden = AuthoredPost.objects.create(title="hidden", author=bob)
    active.write_relationships(
        [
            _grant(shared, "shared", alice_ref),
            _grant(group, "shared", EDITORS),
            _grant(blocked, "blocked", alice_ref),
            RelationshipTuple(EDITORS.object, "member", alice_ref),
        ]
    )
    expected = {owned.pk, shared.pk, group.pk}
    queryset = AuthoredPost.objects.with_actor(alice)
    assert set(queryset.values_list("pk", flat=True)) == expected
    assert queryset.aggregate(count=Count("pk")) == {"count": 3}
    assert set(
        active.accessible(subject=alice_ref, action="read", resource_type="blog/authoredpost")
    ) == {str(pk) for pk in expected}
    assert not queryset.filter(pk__in=[blocked.pk, hidden.pk]).exists()


def test_field_backed_arrow_obeys_group_grants_and_exclusions(active):
    with sudo(reason="field arrow sharing fixtures"):
        folder = Folder.objects.create(name="shared")
        private_folder = Folder.objects.create(name="private")
        visible = Post.objects.create(title="inherited", folder=folder)
        blocked = Post.objects.create(title="blocked", folder=folder)
        private = Post.objects.create(title="private", folder=private_folder)
    owned = _posts("owned")["owned"]
    active.write_relationships(
        [
            RelationshipTuple(EDITORS.object, "member", ALICE),
            _grant(folder, "viewer", EDITORS),
            _grant(blocked, "blocked", ALICE),
            _grant(owned, "owner", ALICE),
        ]
    )
    queryset = Post.objects.with_actor(ALICE).with_action("inherited_read")
    _assert_post_visibility(active, queryset, ALICE, "inherited_read", [visible, owned])
    assert not queryset.filter(pk=private.pk).exists()


def test_rebinding_composed_permission_replaces_actor_and_action(active, shared_posts):
    original = Post.objects.with_actor(ALICE).exclude(title="hidden").scoped_for_aggregate()
    _assert_post_visibility(
        active,
        original,
        ALICE,
        "read",
        [shared_posts[name] for name in ("owned", "shared", "both")],
    )
    with actor_context(ALICE), sudo(reason="explicit actor must override ambient bypass"):
        rebound = original.with_actor(BOB)
        _assert_post_visibility(active, rebound, BOB, "read", [shared_posts["other"]])
        assert rebound.filter(title="hidden").count() == 0
    _assert_post_visibility(
        active,
        original.with_action("union_read"),
        ALICE,
        "union_read",
        [shared_posts[name] for name in ("owned", "shared", "both", "unapproved", "blocked")],
    )
    assert set(original.values_list("title", flat=True)) == {"owned", "shared", "both"}


@pytest.mark.parametrize(
    ("operation", "visible"),
    [("union", {"shared", "other"}), ("intersection", {"shared"}), ("difference", set())],
)
def test_rebinding_sql_set_operands_preserves_caller_filters(
    active, shared_posts, operation, visible
):
    base = Post.objects.with_actor(ALICE).with_action("union_read")
    left = base.filter(title__in=["owned", "shared"]).scoped()
    right = base.filter(title__in=["shared", "other"]).scoped()
    combined = getattr(left, operation)(right).with_actor(BOB)
    assert set(combined.values_list("title", flat=True)) == visible
    assert set(left.values_list("title", flat=True)) == {"owned", "shared"}
    assert set(right.values_list("title", flat=True)) == {"shared"}


def test_direct_share_revocation_refreshes_pending_and_rebound_querysets(active):
    post = _posts("shared")["shared"]
    share = _grant(post, "shared", ALICE)
    active.write_relationships([share, _grant(post, "approved", ALICE)])
    with evaluator_scope() as evaluator:
        assert evaluator.accessible(
            active, subject=ALICE, action="read", resource_type="blog/post"
        ) == (str(post.pk),)
        pending = Post.objects.with_actor(ALICE)
        eager = pending.scoped()
        assert list(eager.values_list("pk", flat=True)) == [post.pk]
        active.delete_relationship(share)
        _assert_post_visibility(active, pending, ALICE, "read", [])
        _assert_post_visibility(active, eager.with_actor(ALICE), ALICE, "read", [])
        assert (
            evaluator.accessible(active, subject=ALICE, action="read", resource_type="blog/post")
            == ()
        )
        active.write_relationships([share])
        _assert_post_visibility(active, pending.all(), ALICE, "read", [post])


def test_group_membership_revocation_preserves_independent_shares(active):
    posts = _posts("group", "nested", "overlap", "other")
    membership = RelationshipTuple(EDITORS.object, "member", ALICE)
    direct = _grant(posts["overlap"], "shared", ALICE)
    active.write_relationships(
        [
            membership,
            RelationshipTuple(EDITORS.object, "member", BOB),
            RelationshipTuple(TEAM.object, "member", EDITORS),
            _grant(posts["group"], "shared", EDITORS),
            _grant(posts["nested"], "shared", TEAM),
            _grant(posts["overlap"], "shared", EDITORS),
            direct,
            _grant(posts["other"], "shared", BOB),
        ]
    )
    with evaluator_scope():
        queryset = Post.objects.with_actor(ALICE).with_action("union_read")
        _assert_post_visibility(
            active,
            queryset,
            ALICE,
            "union_read",
            [posts[name] for name in ("group", "nested", "overlap")],
        )
        active.delete_relationship(membership)
        _assert_post_visibility(active, queryset.all(), ALICE, "union_read", [posts["overlap"]])
        active.delete_relationship(direct)
        _assert_post_visibility(active, queryset.all(), ALICE, "union_read", [])
        _assert_post_visibility(
            active, queryset.with_actor(BOB), BOB, "union_read", list(posts.values())
        )


def test_adding_and_revoking_exclusion_changes_visibility_in_same_evaluator(active):
    post = _posts("shared")["shared"]
    blocked = _grant(post, "blocked", ALICE)
    active.write_relationships([_grant(post, "shared", ALICE), _grant(post, "approved", ALICE)])
    with evaluator_scope():
        queryset = Post.objects.with_actor(ALICE)
        _assert_post_visibility(active, queryset, ALICE, "read", [post])
        active.write_relationships([blocked])
        _assert_post_visibility(active, queryset.all(), ALICE, "read", [])
        active.delete_relationship(blocked)
        _assert_post_visibility(active, queryset.all(), ALICE, "read", [post])


def test_conditional_exclusion_fails_closed_like_graph_evaluation(active):
    active.set_schema(
        parse_zed(
            """
            caveat approved(allowed bool) { allowed }
            definition auth/user {}
            definition blog/post {
                relation shared: auth/user
                relation blocked: auth/user with approved
                permission read = shared - blocked
            }
            """
        )
    )
    posts = _posts("visible", "conditional")
    active.write_relationships(
        [
            *[_grant(post, "shared", ALICE) for post in posts.values()],
            RelationshipTuple(
                to_object_ref(posts["conditional"]), "blocked", ALICE, caveat_name="approved"
            ),
        ]
    )
    result = active.check_access(
        subject=ALICE, action="read", resource=to_object_ref(posts["conditional"])
    )
    assert result.result == PermissionResult.CONDITIONAL_PERMISSION
    _assert_post_visibility(
        active, Post.objects.with_actor(ALICE), ALICE, "read", [posts["visible"]]
    )


def test_recursive_groups_keep_cycle_and_revocation_semantics(active):
    active.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition auth/group {
                relation member: auth/user | auth/group#member
            }
            definition blog/post {
                relation shared: auth/group#member
                permission read = shared
            }
            """
        )
    )
    posts = _posts("shared", "hidden")
    second_group = SubjectRef.of("auth/group", "second", "member")
    membership = RelationshipTuple(second_group.object, "member", ALICE)
    active.write_relationships(
        [
            membership,
            RelationshipTuple(EDITORS.object, "member", second_group),
            RelationshipTuple(second_group.object, "member", EDITORS),
            _grant(posts["shared"], "shared", EDITORS),
        ]
    )
    queryset = Post.objects.with_actor(ALICE)
    _assert_post_visibility(active, queryset, ALICE, "read", [posts["shared"]])
    active.delete_relationship(membership)
    with pytest.raises(PermissionDepthExceeded):
        list(active.accessible(subject=ALICE, action="read", resource_type="blog/post"))
    with pytest.raises(PermissionDepthExceeded):
        list(queryset.all())


@pytest.mark.parametrize("scope_method", ["scoped", "scoped_for_aggregate"])
def test_unevaluated_eager_scope_observes_revoked_group_membership(active, scope_method):
    post = _posts("group-shared")["group-shared"]
    membership = RelationshipTuple(EDITORS.object, "member", ALICE)
    active.write_relationships(
        [membership, _grant(post, "shared", EDITORS), _grant(post, "approved", ALICE)]
    )
    with evaluator_scope():
        eager = getattr(Post.objects.with_actor(ALICE), scope_method)()
        projection = eager.values("pk")
        active.delete_relationship(membership)
        assert list(Post._base_manager.filter(pk__in=projection)) == []
        _assert_post_visibility(active, eager, ALICE, "read", [])


def test_field_owner_sql_cost_is_independent_of_visible_row_count(active):
    alice = get_user_model().objects.create_user(username="bulk-owner")
    bob = get_user_model().objects.create_user(username="other-owner")

    def measure(expected):
        with evaluator_scope():
            # Warm the evaluator's schema snapshot before comparing row queries.
            AuthoredPost.objects.with_actor(alice).count()
            with CaptureQueriesContext(connection) as queries:
                queryset = AuthoredPost.objects.with_actor(alice).scoped_for_aggregate()
                _sql, parameters = queryset.query.sql_with_params()
                assert queryset.count() == expected
                page = list(queryset.order_by("pk").values_list("pk", flat=True)[:25])
                assert len(page) == min(expected, 25)
            return len(queries), len(parameters)

    with sudo(reason="bounded permission SQL fixtures"):
        AuthoredPost.objects.create(title="first", author=alice)
        AuthoredPost.objects.create(title="hidden", author=bob)
    with patch.object(active, "accessible", side_effect=AssertionError("enumerated owned IDs")):
        small_cost = measure(1)
        with sudo(reason="grow bounded permission SQL fixtures"):
            AuthoredPost.objects.bulk_create(
                [AuthoredPost(title=f"owned-{index}", author=alice) for index in range(2000)]
                + [AuthoredPost(title=f"hidden-{index}", author=bob) for index in range(2000)]
            )
        large_cost = measure(2001)
    assert large_cost == small_cost
    assert large_cost[0] == 2  # One aggregate and one bounded page.
    assert large_cost[1] < 100  # Parameters describe the schema, not the 2,001 visible IDs.


def test_stored_arrow_resolves_virtual_targets_without_row_multiplication(active):
    active.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition auth/group { relation member: auth/user }
            definition work/container {
                relation viewer: auth/user | auth/group#member
                permission read = viewer
            }
            definition blog/post {
                relation container: work/container
                relation blocked: auth/user
                permission read = container->read - blocked
            }
            """
        )
    )
    posts = _posts("first", "overlap", "blocked", "hidden", "dangling")
    container = SubjectRef.of("work/container", "folder-A")
    other_container = SubjectRef.of("work/container", "folder-B")
    active.write_relationships(
        [
            RelationshipTuple(EDITORS.object, "member", ALICE),
            RelationshipTuple(container.object, "viewer", EDITORS),
            RelationshipTuple(other_container.object, "viewer", ALICE),
            _grant(posts["first"], "container", container),
            _grant(posts["overlap"], "container", container),
            _grant(posts["overlap"], "container", other_container),
            _grant(posts["blocked"], "container", container),
            _grant(posts["blocked"], "blocked", ALICE),
            _grant(posts["dangling"], "container", SubjectRef.of("work/container", "missing")),
        ]
    )
    _assert_post_visibility(
        active, Post.objects.with_actor(ALICE), ALICE, "read", [posts["first"], posts["overlap"]]
    )
    assert not Post.objects.with_actor(BOB).exists()


def test_constant_arrow_respects_exclusions_and_membership_revocation(active):
    active.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition work/role { relation member: auth/user }
            definition blog/post {
                relation owner: auth/user
                relation admin: work/role // rebac:const=admin
                relation blocked: auth/user
                permission read = (owner + admin->member) - blocked
                permission role_identity = admin
            }
            """
        )
    )
    posts = _posts("shared", "owned", "blocked")
    admin = SubjectRef.of("work/role", "admin")
    membership = RelationshipTuple(admin.object, "member", ALICE)
    active.write_relationships(
        [
            membership,
            RelationshipTuple(ObjectRef("work/role", "other"), "member", BOB),
            _grant(posts["owned"], "owner", ALICE),
            _grant(posts["blocked"], "blocked", ALICE),
        ]
    )
    eager = Post.objects.with_actor(ALICE).scoped_for_aggregate()
    _assert_post_visibility(
        active, Post.objects.with_actor(ALICE), ALICE, "read", [posts["shared"], posts["owned"]]
    )
    assert not Post.objects.with_actor(BOB).exists()
    active.delete_relationship(membership)
    _assert_post_visibility(active, eager, ALICE, "read", [posts["owned"]])
    assert Post.objects.with_actor(admin).with_action("role_identity").count() == 3
    assert (
        not Post.objects.with_actor(SubjectRef.of("work/role", "other"))
        .with_action("role_identity")
        .exists()
    )


def test_queryset_ignores_stale_tuples_outside_declared_subject_shapes(active):
    active.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition auth/group { relation member: auth/user }
            definition blog/post {
                relation exact: auth/user:alice
                relation public: auth/user:*
                relation direct: auth/user
                relation group: auth/group:editors#member
                permission read = ((exact + public) + direct) + group
            }
            """
        )
    )
    posts = _posts(
        "public",
        "direct",
        "exact",
        "group",
        "public-specific",
        "direct-wildcard",
        "direct-subject-set",
        "exact-other",
        "group-other",
        "wrong-type",
    )
    active.write_relationships(
        [
            RelationshipTuple(EDITORS.object, "member", ALICE),
            RelationshipTuple(ObjectRef("auth/group", "other"), "member", ALICE),
            _grant(posts["public"], "public", SubjectRef.of("auth/user", "*")),
            _grant(posts["direct"], "direct", ALICE),
            _grant(posts["exact"], "exact", ALICE),
            _grant(posts["group"], "group", EDITORS),
        ]
    )
    # Persisted grants may outlive a schema tightening; read validation must
    # reject shapes that the current write API would no longer accept.
    for title, relation, subject in (
        ("public-specific", "public", ALICE),
        ("direct-wildcard", "direct", SubjectRef.of("auth/user", "*")),
        ("direct-subject-set", "direct", SubjectRef.of("auth/user", "alice", "member")),
        ("exact-other", "exact", BOB),
        ("group-other", "group", SubjectRef.of("auth/group", "other", "member")),
        ("wrong-type", "direct", SubjectRef.of("auth/group", "alice")),
    ):
        active_relationship_model().objects.create(
            resource_type="blog/post",
            resource_id=str(posts[title].pk),
            relation=relation,
            subject_type=subject.subject_type,
            subject_id=subject.subject_id,
            optional_subject_relation=subject.optional_relation,
        )
    _assert_post_visibility(
        active,
        Post.objects.with_actor(ALICE),
        ALICE,
        "read",
        [posts[name] for name in ("public", "direct", "exact", "group")],
    )
    _assert_post_visibility(active, Post.objects.with_actor(BOB), BOB, "read", [posts["public"]])
    assert not Post.objects.with_actor(SubjectRef.of("auth/user", "alice", "member")).exists()
    assert not Post.objects.with_actor(SubjectRef.of("service/client", "alice")).exists()


def test_expiring_grants_and_exclusions_remain_live_in_eager_querysets(active):
    active.set_schema(
        parse_zed(
            """
            use expiration
            definition auth/user {}
            definition blog/post {
                relation viewer: auth/user with expiration
                relation blocked: auth/user with expiration
                permission read = viewer - blocked
            }
            """
        )
    )
    posts = _posts("permanent", "future", "expired", "blocked", "block-expired")
    past = timezone.now() - timedelta(days=1)
    future = timezone.now() + timedelta(days=1)
    active.write_relationships(
        [
            _grant(posts["permanent"], "viewer", ALICE),
            RelationshipTuple(to_object_ref(posts["future"]), "viewer", ALICE, expires_at=future),
            RelationshipTuple(to_object_ref(posts["expired"]), "viewer", ALICE, expires_at=past),
            _grant(posts["blocked"], "viewer", ALICE),
            _grant(posts["block-expired"], "viewer", ALICE),
            RelationshipTuple(to_object_ref(posts["blocked"]), "blocked", ALICE, expires_at=future),
            RelationshipTuple(
                to_object_ref(posts["block-expired"]), "blocked", ALICE, expires_at=past
            ),
        ]
    )
    eager = Post.objects.with_actor(ALICE).scoped()
    _assert_post_visibility(
        active,
        Post.objects.with_actor(ALICE),
        ALICE,
        "read",
        [posts[name] for name in ("permanent", "future", "block-expired")],
    )
    active.write_relationships(
        [RelationshipTuple(to_object_ref(posts["future"]), "viewer", ALICE, expires_at=past)]
    )
    _assert_post_visibility(
        active, eager, ALICE, "read", [posts["permanent"], posts["block-expired"]]
    )


def test_permission_alias_cycles_fall_back_without_losing_positive_branches(active):
    post = _posts("shared")["shared"]
    for permissions, expected in (
        ("permission read = read", []),
        ("permission read = alias permission alias = read", []),
        ("permission read = loop + viewer permission loop = loop", [post]),
    ):
        active.set_schema(
            parse_zed(
                "definition auth/user {} definition blog/post { relation viewer: auth/user "
                + permissions
                + " }"
            )
        )
        active.write_relationships([_grant(post, "viewer", ALICE)])
        assert active.has_access(
            subject=ALICE, action="read", resource=to_object_ref(post)
        ) == bool(expected)
        with patch.object(active, "accessible", wraps=active.accessible) as enumerate_resources:
            assert list(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == [
                row.pk for row in expected
            ]
        enumerate_resources.assert_called_once()
        assert enumerate_resources.call_args.kwargs["action"] == "read"
        _assert_post_visibility(active, Post.objects.with_actor(ALICE), ALICE, "read", expected)


def test_stored_arrow_to_field_owner_correlates_target_resource_identity(active):
    active.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/authoredpost {
                relation owner: auth/user // rebac:field=author
                permission read = owner
            }
            definition blog/post {
                relation target: blog/authoredpost
                permission read = target->read
            }
            """
        )
    )
    alice = get_user_model().objects.create_user(username="target-owner")
    bob = get_user_model().objects.create_user(username="other-target-owner")
    with sudo(reason="stored arrow into field owner fixtures"):
        authored = AuthoredPost.objects.create(pk=200, title="owned target", author=alice)
        private = AuthoredPost.objects.create(pk=201, title="private target", author=bob)
    posts = _posts("hidden", "visible", "missing")
    active.write_relationships(
        [
            _grant(posts["visible"], "target", SubjectRef(to_object_ref(authored))),
            _grant(posts["hidden"], "target", SubjectRef(to_object_ref(private))),
            _grant(posts["missing"], "target", SubjectRef.of("blog/authoredpost", "99999")),
        ]
    )
    _assert_post_visibility(
        active, Post.objects.with_actor(alice), to_subject_ref(alice), "read", [posts["visible"]]
    )
    _assert_post_visibility(
        active, Post.objects.with_actor(bob), to_subject_ref(bob), "read", [posts["hidden"]]
    )
