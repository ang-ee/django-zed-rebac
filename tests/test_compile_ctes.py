"""Statement-local CTEs share subjects and preserve empty negated arms."""

import gc
import re
from contextlib import nullcontext

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models
from django.db.models import F, Q, Subquery
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    evaluator_scope,
    sudo,
    to_object_ref,
)
from rebac.compile import read
from rebac.errors import PermissionDenied
from rebac.expressions import permission_expression
from rebac.managers import share_scoped_definitions
from rebac.models import active_relationship_model
from rebac.schema import parse_zed
from rebac.testing import install_schema
from tests.testapp.models import Attachment, Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


@pytest.fixture(params=["inline", "decided"])
def plan(request, monkeypatch):
    if request.param == "inline":
        monkeypatch.setattr(read, "_scope_decide", lambda *_args, **_kwargs: False)
    with evaluator_scope() if request.param == "decided" else nullcontext():
        yield request.param


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


@pytest.mark.pg_delta
def test_repeated_recursive_arrow_shares_its_target_cte(monkeypatch):
    monkeypatch.setattr(read, "_scope_decide", lambda *_args, **_kwargs: False)
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
        queryset = Post.objects.with_actor(actor).with_action(action).scoped()
        assert queryset.exists()
        sql = str(queryset.query)
        return len(re.findall(r'"rebac_closure_[^\"]+" AS \(', sql)), sql

    once, once_sql = shape("once")
    many, many_sql = shape("many")
    # After evaluation, the bounded folder target has no subject CTE. Its
    # repeated recursive residual uses one keyed closure CTE per statement.
    assert once == 1
    assert many == once
    assert len(re.findall(r'"rebac_closure_[^\"]+" AS \(', once_sql)) == 1
    assert len(re.findall(r'"rebac_closure_[^\"]+" AS \(', many_sql)) == 1
    separate = sum(shape(action)[0] for action in ("once", "many") * 3)
    assert separate == 6
    expressions = [
        local.permission_expression(
            model=Post, subject=actor, action=action, using="default", identity=F("pk")
        )
        for action in ("once", "many", "once", "many", "once", "many")
    ]
    combined = Post._base_manager.filter(Q(*expressions))
    together = len(re.findall(r'"rebac_[^\"]+" AS \(', str(combined.query)))
    assert together == 12  # plain late expressions compile self-contained
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
    union_count = len(re.findall(r'"rebac_closure_[^\"]+" AS \(', str(unioned.query)))
    # A RebacQuerySet union applies the left action to each operand.
    assert union_count <= once + 2
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
    shared_sql = str(shared_outer.query)
    names = re.findall(r'"(rebac_[^\"]+)" AS \(', shared_sql)
    after = len(names)
    # Unprepared branches render inline, with a subject and closure CTE each.
    assert before == 2 * separate
    # The shared owner keeps one recursive target CTE across all six branches.
    assert sum(name.startswith("rebac_closure_") for name in names) == 1
    assert after == 4 + (connection.vendor == "postgresql")
    assert 0 < after < before
    assert len(names) == len(set(names))
    assert all(shared_sql.count(f'"{name}"') > 1 for name in names)
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


def test_six_operand_owner_defines_each_subject_set_key_once(monkeypatch):
    monkeypatch.setattr(read, "_scope_decide", lambda *_args, **_kwargs: False)
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            permission read = viewer
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission first = folder->read
            permission second = folder->read
            permission third = folder->read
        }
    """)
    actor = SubjectRef.of("auth/user", "six-operand-ids")
    with sudo(reason="six operand subject CTE fixture"):
        folder = Folder.objects.create(name="shared")
        post = Post.objects.create(title="shared", folder=folder)
    local.write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", actor)])

    branches = [
        Post.objects.with_actor(actor)
        .with_action(action)
        .scoped()
        .annotate(can_first=permission_expression(Post, "first", actor=actor))
        .values("pk", "can_first")
        for action in ("first", "second") * 3
    ]
    plain = [
        models.QuerySet(model=Post, query=branch.query.clone(), using=branch.db)
        for branch in branches
    ]
    union = plain[0].union(*plain[1:], all=True)
    before_sql = str(union.query)
    shared = share_scoped_definitions(union)
    with CaptureQueriesContext(connection) as renders:
        after_sql = str(shared.query)
    assert len(renders) == 0
    names = re.findall(r'"(rebac_ids_(\d+)_(lower|upper)(?:_[^\"]+)?)" AS \(', after_sql)
    keys = {(ordinal, bound) for _name, ordinal, bound in names}
    assert len(names) == len(keys) == 2
    assert len(re.findall(r'"rebac_ids_[^\"]+" AS \(', before_sql)) == 12
    assert list(shared.values_list("pk", "can_first")) == [(post.pk, True)] * 6
    unscoped = Post._base_manager.annotate(
        can_first=permission_expression(Post, "third", actor=actor)
    ).values("pk", "can_first")
    mixed = share_scoped_definitions(plain[0].union(plain[1], unscoped, all=True))
    assert len(mixed.query._rebac_subject_plan[2]) == 2
    assert sum(cte.name.startswith("rebac_ids_") for cte in mixed.query._rebac_subject_plan[1]) == 2
    assert list(mixed.values_list("pk", "can_first")) == [(post.pk, True)] * 3


def test_shared_projection_keeps_row_truth_and_where_joins(monkeypatch):
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            relation editor: auth/user
            permission read = viewer
            permission edit = editor
        }
        definition blog/post {
            relation ancestor: blog/folder // rebac:field=folder__parent
            permission first = ancestor->read
            permission second = ancestor->edit
            permission visible = (first + second)
        }
    """)
    actor = SubjectRef.of("auth/user", "projection-owner")
    other = SubjectRef.of("auth/user", "projection-other")
    with sudo(reason="projection row parity fixture"):
        viewed = Folder.objects.create(name="viewed")
        edited = Folder.objects.create(name="edited")
        viewed_child = Folder.objects.create(name="viewed child", parent=viewed)
        edited_child = Folder.objects.create(name="edited child", parent=edited)
        first = Post.objects.create(title="first", folder=viewed_child)
        second = Post.objects.create(title="second", folder=edited_child)
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(viewed), "viewer", actor),
            RelationshipTuple(to_object_ref(edited), "editor", actor),
        ]
    )

    def union(*, in_where=False, projection_actor=actor):
        branches = []
        for _ in range(2):
            branch = (
                Post.objects.with_actor(actor)
                .with_action("visible")
                .scoped()
                .annotate(can_first=permission_expression(Post, "first", actor=projection_actor))
            )
            if in_where:
                branch = branch.filter(can_first=True)
            branch = branch.values("pk", "can_first")
            branches.append(
                models.QuerySet(model=Post, query=branch.query.clone(), using=branch.db)
            )
        return branches[0].union(branches[1], all=True)

    expected = sorted(union().values_list("pk", "can_first"))
    expected_where = sorted(union(in_where=True).values_list("pk", "can_first"))
    assert expected == sorted([(first.pk, True), (second.pk, False)] * 2)
    assert expected_where == [(first.pk, True)] * 2

    normal_shared = share_scoped_definitions(union())
    assert len(normal_shared.query._rebac_subject_plan[2]) == 2
    assert sorted(normal_shared.values_list("pk", "can_first")) == expected

    original = read.Compiler._subject_cte

    def without_projection_cte(self, key, model, identity, bound, visits, depth_possible):
        if key == ("blog/post", "first"):
            return None
        return original(self, key, model, identity, bound, visits, depth_possible)

    monkeypatch.setattr(read.Compiler, "_subject_cte", without_projection_cte)
    assert sorted(share_scoped_definitions(union()).values_list("pk", "can_first")) == expected
    assert (
        sorted(share_scoped_definitions(union(in_where=True)).values_list("pk", "can_first"))
        == expected_where
    )
    different = share_scoped_definitions(union(projection_actor=other))
    assert different.query._rebac_subject_plan[2] == {}
    assert sorted(different.values_list("pk", "can_first")) == sorted(
        [(first.pk, False), (second.pk, False)] * 2
    )


def test_shared_decided_set_projection_has_no_target_subject_cte():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation member: auth/user | blog/folder#member
        }
        definition blog/post {
            relation viewer: blog/folder#member
            permission read = viewer
            permission check = viewer
        }
    """)
    actor = SubjectRef.of("auth/user", "projected-set")
    with sudo(reason="projected set fixture"):
        folder = Folder.objects.create(name="member set")
        post = Post.objects.create(title="set reader")
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(folder), "member", actor),
            RelationshipTuple(
                to_object_ref(post),
                "viewer",
                SubjectRef.of("blog/folder", str(folder.pk), "member"),
            ),
        ]
    )
    branches = [
        Post.objects.with_actor(actor)
        .with_action("read")
        .scoped()
        .annotate(can_check=permission_expression(Post, "check", actor=actor))
        .values("pk", "can_check")
        for _ in range(3)
    ]
    plain = [
        models.QuerySet(model=Post, query=branch.query.clone(), using=branch.db)
        for branch in branches
    ]
    shared = share_scoped_definitions(plain[0].union(*plain[1:], all=True))
    sql = str(shared.query)
    ids = re.findall(r'"rebac_ids_(\d+)_(lower|upper)(?:_[^\"]+)?" AS \(', sql)
    assert len(ids) == len(set(ids)) == 2
    assert list(shared.values_list("pk", "can_check")) == [(post.pk, True)] * 3


@pytest.fixture
def shared_scope_rows():
    local = install_schema("""
        definition auth/user {}
        definition auth/group { relation member: auth/user | auth/group#member }
        definition blog/folder {
            relation viewer: auth/user | auth/group#member
            permission read = viewer
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
        }
    """)
    actor = SubjectRef.of("auth/user", "shared-scope-regression")
    with sudo(reason="shared scope regression fixture"):
        allowed = Folder.objects.create(name="allowed")
        denied = Folder.objects.create(name="denied")
    group = ObjectRef("auth/group", "shared-scope-regression")
    local.write_relationships(
        [
            RelationshipTuple(group, "member", actor),
            RelationshipTuple(to_object_ref(allowed), "viewer", SubjectRef(group, "member")),
        ]
    )
    return local, actor, allowed, denied


@pytest.fixture
def late_shared_rows():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation owner: auth/user
            relation viewer: auth/user
            relation banned: auth/user
            permission read = owner + viewer
            permission write = owner
            permission share = owner - banned
        }
    """)
    actor = SubjectRef.of("auth/user", "late-shared")
    with sudo(reason="late shared fixture"):
        folders = [Folder.objects.create(name=f"late-{index}") for index in range(4)]
        posts = [
            Post.objects.create(title=f"late-{index}", folder=folder)
            for index, folder in enumerate(folders)
        ]
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(folders[0]), "owner", actor),
            RelationshipTuple(to_object_ref(folders[1]), "viewer", actor),
            RelationshipTuple(to_object_ref(folders[2]), "owner", actor),
            RelationshipTuple(to_object_ref(folders[2]), "banned", actor),
        ]
    )
    return actor, folders, posts


@pytest.fixture
def late_reference_rows():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation owner: auth/user
            relation viewer: auth/user
            relation banned: auth/user
            permission read = owner + viewer
            permission write = owner
            permission share = owner - banned
        }
    """)
    actor = SubjectRef.of("auth/user", "late-reference")
    with sudo(reason="late reference fixture"):
        roots = [Folder.objects.create(name=f"root-{index}") for index in range(3)]
        children = [
            Folder.objects.create(name=f"child-{index}", parent=root)
            for index, root in enumerate(roots)
        ]
    local.write_relationships(
        [
            *(RelationshipTuple(to_object_ref(root), "viewer", actor) for root in roots),
            RelationshipTuple(to_object_ref(children[0]), "owner", actor),
            RelationshipTuple(to_object_ref(children[1]), "viewer", actor),
        ]
    )
    return actor, roots, children


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


def test_late_permission_expression_on_scoped_queryset_is_self_contained(shared_arrow_rows, plan):
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


@pytest.mark.pg_delta
def test_independently_shared_operands_have_distinct_cte_names(shared_arrow_rows):
    _local, actor, post = shared_arrow_rows

    def shared_pair():
        first = _plain(Post.objects.with_actor(actor).with_action("once").scoped())
        second = _plain(Post.objects.with_actor(actor).with_action("many").scoped())
        return share_scoped_definitions(first.union(second, all=True))

    left = shared_pair()
    right = shared_pair()
    assert str(left.query) == str(right.query)
    together = left.union(right, all=True)
    names = re.findall(r'"(rebac_[^\"]+)" AS \(', str(together.query))
    assert len(names) == len(set(names))
    assert list(together.values_list("pk", flat=True)) == [post.pk] * 4
    chained = together.union(shared_pair(), all=True)
    chained_names = re.findall(r'"(rebac_[^\"]+)" AS \(', str(chained.query))
    assert len(chained_names) == len(set(chained_names))
    assert list(chained.values_list("pk", flat=True)) == [post.pk] * 6


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_shared_rebac_union_keeps_actor_scope_and_stamps_instances(storage):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        local = install_schema("""
            definition auth/user {}
            definition blog/post {
                relation viewer: auth/user
                permission read = viewer
            }
        """)
        actor = SubjectRef.of("auth/user", "shared-union-reader")
        with sudo(reason="shared union hidden rows fixture"):
            posts = Post.objects.bulk_create([Post(title=f"post-{i}") for i in range(12)])
        visible = posts[::2]
        local.write_relationships(
            [RelationshipTuple(to_object_ref(post), "viewer", actor) for post in visible]
        )
        rows = Post.objects.with_actor(actor)
        union = share_scoped_definitions(
            rows.filter(pk__in=[post.pk for post in posts[:6]]).union(
                rows.filter(pk__in=[post.pk for post in posts[6:]])
            )
        )
        result = list(union.order_by("pk"))
        assert [post.pk for post in result] == [post.pk for post in visible]
        assert all(post._rebac_actor == actor for post in result)


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_nonidentical_shared_owners_relabel_compiled_references(storage):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        local = install_schema("""
            definition auth/user {}
            definition blog/folder {
                relation viewer: auth/user
                relation editor: auth/user
                permission read = viewer
                permission edit = editor
            }
            definition blog/post {
                relation folder: blog/folder // rebac:field=folder
                permission once = folder->read
                permission many = folder->read
                permission read = folder->edit
                permission view = folder->edit
            }
        """)
        actor = SubjectRef.of("auth/user", "independent-owner-reader")
        with sudo(reason="independent owner fixture"):
            first_folder = Folder.objects.create(name="first")
            second_folder = Folder.objects.create(name="second")
            first = Post.objects.create(title="first", folder=first_folder)
            second = Post.objects.create(title="second", folder=second_folder)
        local.write_relationships(
            [
                RelationshipTuple(to_object_ref(first_folder), "viewer", actor),
                RelationshipTuple(to_object_ref(second_folder), "editor", actor),
            ]
        )

        def pair(left, right):
            first_operand = _plain(Post.objects.with_actor(actor).with_action(left).scoped())
            second_operand = _plain(Post.objects.with_actor(actor).with_action(right).scoped())
            return share_scoped_definitions(first_operand.union(second_operand))

        left = pair("once", "many")
        right = pair("read", "view")
        assert {cte.name for cte in left.query._with_ctes} & {
            cte.name for cte in right.query._with_ctes
        }
        assert list(left.values_list("pk", flat=True)) == [first.pk]
        assert list(right.values_list("pk", flat=True)) == [second.pk]
        for operation, expected in (
            ("union", {first.pk, second.pk}),
            ("intersection", set()),
            ("difference", {first.pk}),
        ):
            combined = getattr(left, operation)(right)
            assert set(combined.values_list("pk", flat=True)) == expected
            names = re.findall(r'"(rebac_[^\"]+)" AS \(', str(combined.query))
            assert len(names) == len(set(names))


def test_constant_arrow_shares_used_reached_relation_and_rechecks_fact():
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
    membership = RelationshipTuple(ObjectRef("test/role", "admin"), "viewer", actor)
    local.write_relationships([membership])
    read = _plain(Post.objects.with_actor(actor).with_action("read").scoped())
    view = _plain(Post.objects.with_actor(actor).with_action("view").scoped())
    shared = share_scoped_definitions(read.union(view, all=True))
    assert getattr(shared.query, "_rebac_subject_plan", None) is not None
    sql = str(shared.query)
    names = re.findall(r'"(rebac_reached_[^\"]+)" AS \(', sql)
    assert len(names) == len(set(names)) == 1
    assert sql.count(f'"{names[0]}"') > 1
    assert list(shared.values_list("pk", flat=True)) == [post.pk] * 2
    local.delete_relationship(membership)
    assert list(shared.all().values_list("pk", flat=True)) == []


@pytest.mark.django_db(transaction=True)
def test_shared_decision_witnesses_nested_revocation():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation parent: blog/folder // rebac:field=parent
            relation viewer: auth/user
            permission view = viewer
            permission read = parent->view
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
            permission view = folder->read
        }
    """)
    actor = SubjectRef.of("auth/user", "shared-revocation")
    with sudo(reason="shared witness fixture"):
        top = Folder.objects.create(name="top")
        child = Folder.objects.create(name="child", parent=top)
        post = Post.objects.create(title="child", folder=child)
    local.write_relationships([RelationshipTuple(to_object_ref(top), "viewer", actor)])
    read = _plain(Post.objects.with_actor(actor).with_action("read").scoped())
    view = _plain(Post.objects.with_actor(actor).with_action("view").scoped())
    shared = share_scoped_definitions(read.union(view, all=True))
    assert list(shared.values_list("pk", flat=True)) == [post.pk] * 2
    table = active_relationship_model()._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(f'DELETE FROM "{table}" WHERE "relation" = %s', ["viewer"])
    assert list(shared.all().values_list("pk", flat=True)) == []


@pytest.mark.django_db(transaction=True)
def test_shared_actor_set_witness_catches_new_excluded_membership():
    local = install_schema("""
        definition auth/user {}
        definition auth/group { relation member: auth/user }
        definition blog/post {
            relation viewer: auth/user
            relation banned: auth/group#member
            permission read = viewer - banned
            permission view = viewer - banned
        }
    """)
    actor = SubjectRef.of("auth/user", "shared-exclusion")
    with sudo(reason="shared exclusion fixture"):
        post = Post.objects.create(title="excluded")
    group = ObjectRef("auth/group", "excluded")
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(post), "viewer", actor),
            RelationshipTuple(to_object_ref(post), "banned", SubjectRef(group, "member")),
        ]
    )
    read = _plain(Post.objects.with_actor(actor).with_action("read").scoped())
    view = _plain(Post.objects.with_actor(actor).with_action("view").scoped())
    shared = share_scoped_definitions(read.union(view, all=True))
    assert list(shared.values_list("pk", flat=True)) == [post.pk] * 2
    local.write_relationships([RelationshipTuple(group, "member", actor)])
    assert list(shared.all().values_list("pk", flat=True)) == []


@pytest.mark.pg_delta
def test_three_operand_union_prepares_once_and_stays_near_inline(monkeypatch):
    local = install_schema("""
        definition auth/user {}
        definition auth/group { relation member: auth/user | auth/group#member }
        definition site/role { relation member: auth/user | auth/group#member }
        definition blog/folder {
            relation parent: blog/folder // rebac:field=parent
            relation viewer: auth/user | auth/group#member
            relation admin: site/role // rebac:const=admin
            permission read = viewer + parent->read + admin->member
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            relation viewer: auth/user | auth/group#member
            relation admin: site/role // rebac:const=admin
            permission read = viewer + folder->read + admin->member
        }
        definition test/attachment {
            relation target: blog/post // rebac:field=target
            relation post: blog/post // rebac:field=object_id
            relation viewer: auth/user | auth/group#member
            permission read = viewer + target->read
        }
    """)
    actor = SubjectRef.of("auth/user", "three-operand")
    with sudo(reason="three-operand fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        other = Folder.objects.create(name="other")
        post = Post.objects.create(title="visible", folder=child)
        hidden = Post.objects.create(title="hidden", folder=other)
        content_type = ContentType.objects.get_for_model(Post)
        Attachment.objects.create(content_type=content_type, object_id=post.pk)
        Attachment.objects.create(content_type=content_type, object_id=hidden.pk)
    group = ObjectRef("auth/group", "readers")
    local.write_relationships(
        [
            RelationshipTuple(group, "member", actor),
            RelationshipTuple(to_object_ref(root), "viewer", SubjectRef(group, "member")),
        ]
    )

    def statement():
        with CaptureQueriesContext(connection) as branches:
            scoped = [
                model.objects.with_actor(actor)
                .scoped()
                .order_by()
                .annotate(_kind=models.Value(index, output_field=models.IntegerField()))
                .values("_kind", "pk")
                for index, model in enumerate((Folder, Post, Attachment))
            ]
        first, *rest = scoped
        plain = models.QuerySet(model=first.model, query=first.query.clone(), using=first.db)
        with CaptureQueriesContext(connection) as prepared:
            combined = share_scoped_definitions(plain.union(*rest, all=True).order_by())
        with CaptureQueriesContext(connection) as rendered:
            str(combined.query)
        with CaptureQueriesContext(connection) as executed:
            result = sorted(combined.values_list("_kind", "pk"))
        assert len(branches) == 0
        assert len(rendered) == 0
        assert len(executed) == 1
        return result, len(executed[0]["sql"].encode()), len(prepared)

    decided_rows, decided_bytes, preparation = statement()
    assert preparation == 4
    with monkeypatch.context() as inline:
        inline.setattr(read._Rows, "rows", lambda self, key: None)
        inline.setattr(read._Operation, "sets", lambda self, verdicts: None)
        inline.setattr(read, "const_facts", lambda *args: ())
        read.reset()
        inline_rows, inline_bytes, inline_preparation = statement()
    assert inline_preparation == 0
    assert decided_rows == inline_rows
    print(f"three-operand union bytes: composed={decided_bytes}, R2={inline_bytes}")
    # PostgreSQL UNION: 16,055 B composed versus 15,452 B inline, a 603 B
    # gap after binding the recursive decided membership. The witness budget
    # is a ceiling, not an exact byte target.
    assert decided_bytes <= inline_bytes + (625 if connection.vendor == "postgresql" else 0)


@pytest.mark.pg_delta
@pytest.mark.parametrize("count", [2, pytest.param(300, marks=pytest.mark.slow)])
def test_live_unions_relabel_names_after_owner_queries_are_collected(shared_arrow_rows, count):
    _local, actor, post = shared_arrow_rows
    # Two independent live owners are the minimum that can collide when the
    # first owner queryset is collected but its cloned query remains live.
    live = []
    first_names = None
    for _ in range(count):
        first = _plain(Post.objects.with_actor(actor).with_action("once").scoped())
        second = _plain(Post.objects.with_actor(actor).with_action("many").scoped())
        owner = share_scoped_definitions(first.union(second, all=True))
        clone = owner.order_by()
        names = tuple(sorted(re.findall(r'"(rebac_[^\"]+)" AS \(', str(clone.query))))
        assert names
        if first_names is None:
            first_names = names
        else:
            assert names == first_names
        live.append(clone)
        del owner
        gc.collect()
    combined = live[0].union(live[-1], all=True)
    names = re.findall(r'"(rebac_[^\"]+)" AS \(', str(combined.query))
    assert len(names) == len(set(names))
    assert list(combined.values_list("pk", flat=True)) == [post.pk] * 4


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


_UNION_OPERAND_SCHEMA = """
definition auth/user {}
definition blog/folder {
  relation owner: auth/user
  relation viewer: auth/user
  relation banned: auth/user
  permission read = owner + viewer
  permission write = owner
  permission share = owner - banned
}
"""
