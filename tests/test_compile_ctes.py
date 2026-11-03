"""Statement-local CTEs share subjects and preserve empty negated arms."""

import gc
import re

import pytest
from django.db import connection, models
from django.db.models import F, Q, Subquery
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.errors import PermissionDenied
from rebac.managers import share_scoped_definitions
from rebac.schema import parse_zed
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db


def test_empty_subject_set_in_negated_arm_keeps_outer_row():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            permission ban = nil
        }
        definition blog/post {
            relation viewer: auth/user
            relation folders: blog/folder // rebac:field=collections
            permission read = viewer - folders->ban
        }
    """)
    actor = SubjectRef.of("auth/user", "empty-cte")
    with sudo(reason="empty CTE fixture"):
        folder = Folder.objects.create(name="folder")
        post = Post.objects.create(title="visible")
        post.collections.add(folder)
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", actor)])
    assert list(Post.objects.with_actor(actor).values_list("pk", flat=True)) == [post.pk]
    assert local.check_access(subject=actor, action="read", resource=to_object_ref(post)).allowed


def test_repeated_recursive_arrow_shares_its_target_cte():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            relation parent: blog/folder
            permission read = viewer + parent->read
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            relation folders: blog/folder // rebac:field=collections
            permission once = (folder->read + folders->read)
            permission many = (folder->read + folders->read) + (folder->read + folders->read) + (folder->read + folders->read) + (folder->read + folders->read)
        }
    """)
    actor = SubjectRef.of("auth/user", "shared-cte")
    with sudo(reason="shared CTE fixture"):
        folder = Folder.objects.create(name="shared")
        post = Post.objects.create(title="shared", folder=folder)
        post.collections.add(folder)
    local.write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", actor)])

    def shape(action):
        sql = str(Post.objects.with_actor(actor).with_action(action).scoped().query)
        return len(re.findall(r'"rebac_[^\"]+" AS \(', sql)), sql

    once, _ = shape("once")
    many, _ = shape("many")
    assert once == 2
    assert many == once
    separate = sum(shape(action)[0] for action in ("once", "many") * 3)
    assert separate == 12
    expressions = [
        local.permission_expression(
            model=Post, subject=actor, action=action, using="default", identity=F("pk")
        )
        for action in ("once", "many", "once", "many", "once", "many")
    ]
    combined = Post._base_manager.filter(Q(*expressions))
    together = len(re.findall(r'"rebac_[^\"]+" AS \(', str(combined.query)))
    assert together == separate  # plain late expressions compile self-contained
    assert list(combined.values_list("pk", flat=True)) == [post.pk]
    base = Post._base_manager.filter(
        Q(
            local.permission_expression(
                model=Post, subject=actor, action="once", using="default", identity=F("pk")
            )
        )
    )
    extended = base.filter(
        Q(
            local.permission_expression(
                model=Post, subject=actor, action="many", using="default", identity=F("pk")
            )
        )
    )
    assert list(base.values_list("pk", flat=True)) == [post.pk]
    assert list(extended.values_list("pk", flat=True)) == [post.pk]
    branches = [
        Post.objects.with_actor(actor).with_action(action).scoped().values("pk")
        for action in ("once", "many", "once", "many", "once", "many")
    ]
    unioned = branches[0].union(*branches[1:])
    union_count = len(re.findall(r'"rebac_[^\"]+" AS \(', str(unioned.query)))
    # A RebacQuerySet union applies the left action to each operand.
    assert union_count == once + 1  # one root CTE and the shared recursive target
    assert list(unioned.values_list("pk", flat=True)) == [post.pk]
    # The Timeline owner deliberately wraps each independently scoped branch
    # in a plain QuerySet so Django's REBAC combinators cannot rebind its action.
    plain = [
        models.QuerySet(model=Post, query=branch.query.clone(), using=branch.db).values("pk")
        for branch in branches
    ]
    outer = plain[0].union(*plain[1:], all=True)
    before = len(re.findall(r'"rebac_[^\"]+" AS \(', str(outer.query)))
    shared_outer = share_scoped_definitions(outer)
    names = re.findall(r'"(rebac_[^\"]+)" AS \(', str(shared_outer.query))
    after = len(names)
    assert before == 12  # six independent operands each carry two definitions
    assert after == 4  # two root permissions, the folder target and its recursion
    assert len(names) == len(set(names))
    assert list(shared_outer.values_list("pk", flat=True)) == [post.pk] * 6
    different = (
        Post.objects.with_actor(SubjectRef.of("auth/user", "other")).with_action("once").scoped()
    )
    different_plain = models.QuerySet(model=Post, query=different.query.clone(), using=different.db)
    with pytest.raises(PermissionDenied, match="compatible prepared REBAC scopes"):
        share_scoped_definitions(plain[0].union(different_plain.values("pk"), all=True))
    unscoped = plain[0].union(models.QuerySet(model=Post, using="default").values("pk"), all=True)
    assert list(share_scoped_definitions(unscoped).values_list("pk", flat=True)) == [post.pk] * 2
    embedded = models.QuerySet(model=Post, using="default").filter(
        pk__in=Subquery(unioned.values("pk"))
    )
    assert list(embedded.values_list("pk", flat=True)) == [post.pk]
    folder_scopes = [
        local.permission_expression(
            model=Folder, subject=actor, action="read", using="default", identity=F("pk")
        )
        for _ in range(4)
    ]
    folder_query = Folder._base_manager.filter(Q(*folder_scopes))
    assert len(re.findall(r'"rebac_[^\"]+" AS \(', str(folder_query.query))) == 4
    assert list(folder_query.values_list("pk", flat=True)) == [folder.pk]
    assert list(
        Post.objects.with_actor(actor).with_action("many").values_list("pk", flat=True)
    ) == [post.pk]


def test_late_nested_set_scopes_compile_self_contained():
    local = install_schema("""
        definition auth/user {}
        definition auth/group {
            relation member: auth/user | auth/group#member
        }
        definition blog/post {
            relation viewer: auth/group#member
            permission read = viewer
        }
    """)
    actor = SubjectRef.of("auth/user", "nested-cte")
    with sudo(reason="nested CTE fixture"):
        post = Post.objects.create(title="nested")
    inner = ObjectRef("auth/group", "inner")
    outer = ObjectRef("auth/group", "outer")
    local.write_relationships(
        [
            RelationshipTuple(inner, "member", actor),
            RelationshipTuple(outer, "member", SubjectRef.of("auth/group", "inner", "member")),
            RelationshipTuple(
                to_object_ref(post), "viewer", SubjectRef.of("auth/group", "outer", "member")
            ),
        ]
    )
    scopes = [
        local.permission_expression(
            model=Post, subject=actor, action="read", using="default", identity=F("pk")
        )
        for _ in range(4)
    ]
    rows = Post._base_manager.filter(Q(*scopes))
    with CaptureQueriesContext(connection) as queries:
        sql = str(rows.query)
    assert len(queries) == 0
    assert len(re.findall(r'"rebac_[^\"]+" AS \(', sql)) == 4
    assert len(re.findall(r'"rebac_sets_[^\"]+" AS \(', sql)) == 4
    assert list(rows.values_list("pk", flat=True)) == [post.pk]


@pytest.fixture
def shared_arrow_rows():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            relation parent: blog/folder
            permission read = viewer + parent->read
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            relation folders: blog/folder // rebac:field=collections
            permission once = (folder->read + folders->read)
            permission many = (folder->read + folders->read) + folder->read
        }
    """)
    actor = SubjectRef.of("auth/user", "regression")
    with sudo(reason="shared arrow regression"):
        folder = Folder.objects.create(name="regression")
        post = Post.objects.create(title="regression", folder=folder)
        post.collections.add(folder)
    local.write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", actor)])
    return local, actor, post


def _plain(rows):
    return models.QuerySet(model=rows.model, query=rows.query.clone(), using=rows.db).values("pk")


def test_late_permission_expression_on_scoped_queryset_is_self_contained(shared_arrow_rows):
    local, actor, post = shared_arrow_rows
    expression = local.permission_expression(
        model=Post, subject=actor, action="many", using="default", identity=F("pk")
    )
    rows = Post.objects.with_actor(actor).with_action("once").scoped().annotate(can_many=expression)
    assert list(rows.values_list("pk", "can_many")) == [(post.pk, True)]


def test_boolean_sibling_clones_keep_their_own_definitions(shared_arrow_rows):
    local, actor, post = shared_arrow_rows

    def permission(action):
        return Q(
            local.permission_expression(
                model=Post, subject=actor, action=action, using="default", identity=F("pk")
            )
        )

    base = Post._base_manager.filter(permission("once"), permission("many"))
    sibling = base.filter(permission("once"))
    assert list((base | sibling).values_list("pk", flat=True)) == [post.pk]
    assert list((base & sibling).values_list("pk", flat=True)) == [post.pk]
    assert list(base.values_list("pk", flat=True)) == [post.pk]


def test_rebac_union_leaves_left_operand_executable(shared_arrow_rows):
    _local, actor, post = shared_arrow_rows
    left = Post.objects.with_actor(actor).with_action("once").scoped().values("pk")
    right = Post.objects.with_actor(actor).with_action("many").scoped().values("pk")
    unioned = left.union(right)
    str(unioned.query)
    assert list(left.values_list("pk", flat=True)) == [post.pk]
    assert left.count() == 1
    assert list(unioned.values_list("pk", flat=True)) == [post.pk]


def test_helper_passes_sudo_none_and_single_operands(shared_arrow_rows):
    _local, actor, post = shared_arrow_rows
    scoped = _plain(Post.objects.with_actor(actor).with_action("once").scoped())
    bypass = _plain(Post.objects.sudo(reason="provenance branch").scoped())
    empty = models.QuerySet(model=Post, using="default").none().values("pk")
    unioned = share_scoped_definitions(scoped.union(bypass, empty, all=True))
    assert list(unioned.values_list("pk", flat=True)) == [post.pk] * 2
    assert list(share_scoped_definitions(scoped).values_list("pk", flat=True)) == [post.pk]


def test_independently_shared_operands_have_distinct_cte_names(shared_arrow_rows):
    _local, actor, post = shared_arrow_rows

    def shared_pair():
        first = _plain(Post.objects.with_actor(actor).with_action("once").scoped())
        second = _plain(Post.objects.with_actor(actor).with_action("many").scoped())
        return share_scoped_definitions(first.union(second, all=True))

    left = shared_pair()
    right = shared_pair()
    together = left.union(right, all=True)
    names = re.findall(r'"(rebac_[^\"]+)" AS \(', str(together.query))
    assert len(names) == len(set(names))
    assert list(together.values_list("pk", flat=True)) == [post.pk] * 4


def test_constant_arrow_shares_one_reached_relation():
    local = install_schema("""
        definition auth/user {}
        definition test/role {
            relation viewer: auth/user
            relation parent: test/role
            permission member = viewer + parent->member
        }
        definition blog/post {
            relation admin: test/role // rebac:const=admin
            permission read = admin->member
            permission view = admin->member
        }
    """)
    actor = SubjectRef.of("auth/user", "reached-cte")
    with sudo(reason="reached relation fixture"):
        post = Post.objects.create(title="reached")
    local.write_relationships([RelationshipTuple(ObjectRef("test/role", "admin"), "viewer", actor)])
    read = _plain(Post.objects.with_actor(actor).with_action("read").scoped())
    view = _plain(Post.objects.with_actor(actor).with_action("view").scoped())
    shared = share_scoped_definitions(read.union(view, all=True))
    names = re.findall(r'"(rebac_reached_[^\"]+)" AS \(', str(shared.query))
    assert len(names) == len(set(names)) == 1
    assert list(shared.values_list("pk", flat=True)) == [post.pk] * 2


@pytest.mark.parametrize("count", [32, pytest.param(300, marks=pytest.mark.slow)])
def test_live_unions_keep_distinct_names_after_owner_queries_are_collected(
    shared_arrow_rows, count
):
    _local, actor, post = shared_arrow_rows
    live = []
    names_seen = set()
    for _ in range(count):
        first = _plain(Post.objects.with_actor(actor).with_action("once").scoped())
        second = _plain(Post.objects.with_actor(actor).with_action("many").scoped())
        owner = share_scoped_definitions(first.union(second, all=True))
        clone = owner.order_by()
        names = tuple(sorted(re.findall(r'"(rebac_[^\"]+)" AS \(', str(clone.query))))
        assert names
        assert names not in names_seen
        names_seen.add(names)
        live.append(clone)
        del owner
        gc.collect()
    assert list(live[0].union(live[-1], all=True).values_list("pk", flat=True)) == [post.pk] * 4


def test_policy_revision_mismatch_compiles_each_union_scope_self_contained():
    local = install_schema("""
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user
            permission read = viewer
        }
    """)
    actor = SubjectRef.of("auth/user", "revision-cte")
    with sudo(reason="revision mismatch fixture"):
        post = Post.objects.create(title="revision")
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", actor)])
    old = _plain(Post.objects.with_actor(actor).scoped())
    local.set_schema(
        parse_zed("""
            definition auth/user {}
            definition blog/post {
                relation viewer: auth/user
                permission read = viewer + authenticated
            }
        """)
    )
    current = _plain(Post.objects.with_actor(actor).scoped())
    unioned = share_scoped_definitions(old.union(current, all=True))
    assert getattr(unioned.query, "_rebac_subject_plan", None) is None
    assert list(unioned.values_list("pk", flat=True)) == [post.pk]
