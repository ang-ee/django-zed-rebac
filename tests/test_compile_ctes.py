"""Statement-local CTEs share subjects and preserve empty negated arms."""

import gc
import re

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models
from django.db.models import F, Q, Subquery
from django.test.utils import CaptureQueriesContext

from rebac import (
    SCOPE_CTE_CANDIDATE_LIMIT,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
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
from tests.testapp.models import Attachment, Folder, IntegerIdentityFolder, Post

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
        queryset = Post.objects.with_actor(actor).with_action(action).scoped()
        assert queryset.exists()
        sql = str(queryset.query)
        return len(re.findall(r'"rebac_[^\"]+" AS \(', sql)), sql

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
    union_count = len(re.findall(r'"rebac_[^\"]+" AS \(', str(unioned.query)))
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
    assert after == 4
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


def test_six_operand_owner_defines_each_subject_set_key_once():
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


@pytest.mark.pg_delta
def test_late_nested_set_scopes_share_one_statement_definition():
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
    shared = Post.objects.with_actor(actor).scope_cte()
    sizes = []
    inline_sizes = []
    for count in (1, 3):
        scopes = [
            local.permission_expression(
                model=Post, subject=actor, action="read", using="default", identity=F("pk")
            )
            for _ in range(count)
        ]
        inline = Post._base_manager.filter(Q(*scopes))
        with CaptureQueriesContext(connection) as queries:
            inline_sql = str(inline.query)
        assert len(queries) == 0
        assert len(re.findall(r'"rebac_[^\"]+" AS \(', inline_sql)) == count
        inline_sizes.append(len(inline_sql.encode()))
        inline_rows = list(inline.values_list("pk", flat=True))
        rows = shared.attach(inline)
        with CaptureQueriesContext(connection) as queries:
            sql = str(rows.query)
        assert len(queries) == 0
        assert len(re.findall(r'"rebac_scope_[^\"]+" AS \(', sql)) == 1
        assert len(re.findall(r'"rebac_sets_[^\"]+" AS \(', sql)) == 1
        sizes.append(len(sql.encode()))
        assert list(rows.values_list("pk", flat=True)) == inline_rows == [post.pk]
    assert 0 < sizes[1] - sizes[0] < 400
    assert inline_sizes[1] - inline_sizes[0] > 2_000


@pytest.mark.pg_delta
def test_shared_scope_filter_projection_and_exists_guard():
    local = install_schema("""
        definition auth/user {}
        definition auth/group {
            relation member: auth/user | auth/group#member
        }
        definition blog/folder {
            relation viewer: auth/group#member
            permission read = viewer
            permission write = nil
        }
    """)
    actor = SubjectRef.of("auth/user", "mixed-scope")
    with sudo(reason="shared scope fixture"):
        allowed = Folder.objects.create(name="allowed")
        Folder.objects.create(name="denied")
    group = ObjectRef("auth/group", "mixed-scope")
    local.write_relationships(
        [
            RelationshipTuple(group, "member", actor),
            RelationshipTuple(to_object_ref(allowed), "viewer", SubjectRef(group, "member")),
        ]
    )
    shared = Folder.objects.with_actor(actor).scope_cte()
    base = Folder._base_manager.filter(pk__in=shared.queryset().values("pk"))
    baseline = shared.attach(base)
    mixed = shared.attach(
        base.annotate(
            projected=permission_expression(Folder, "read", actor=actor),
            guarded=models.Exists(
                Folder.objects.with_actor(actor)
                .scoped_for_aggregate()
                .filter(pk=models.OuterRef("pk"))
                .values("pk")
            ),
            referenced=models.Exists(shared.queryset().filter(pk=models.OuterRef("pk"))),
        )
    )
    base_sql = str(baseline.query)
    mixed_sql = str(mixed.query)
    assert len(re.findall(r'"rebac_scope_[^\"]+" AS \(', mixed_sql)) == 1
    assert len(re.findall(r'"rebac_sets_[^\"]+" AS \(', mixed_sql)) == 1
    assert 0 < len(mixed_sql.encode()) - len(base_sql.encode()) < 1200
    assert len(re.findall(r'"rebac_scope_[^\"]+" AS \(', str(shared.attach(mixed).query))) == 1
    assert list(mixed.values_list("pk", "projected", "guarded", "referenced")) == [
        (allowed.pk, True, True, True)
    ]
    assert mixed.aggregate(total=models.Count("pk")) == {"total": 1}
    inline = Folder._base_manager.filter(
        pk__in=Folder.objects.with_actor(actor).scoped_for_aggregate().values("pk")
    ).annotate(
        projected=permission_expression(Folder, "read", actor=actor),
        guarded=models.Exists(
            Folder.objects.with_actor(actor)
            .scoped_for_aggregate()
            .filter(pk=models.OuterRef("pk"))
            .values("pk")
        ),
        referenced=models.Exists(
            Folder.objects.with_actor(actor).scoped_for_aggregate().filter(pk=models.OuterRef("pk"))
        ),
    )
    assert list(mixed.values_list("pk", "projected", "guarded", "referenced")) == list(
        inline.values_list("pk", "projected", "guarded", "referenced")
    )
    assert mixed.count() == inline.count()
    assert mixed.exists() == inline.exists()
    assert mixed.aggregate(total=models.Count("pk")) == inline.aggregate(total=models.Count("pk"))
    other_action = Folder._base_manager.annotate(
        projected=permission_expression(Folder, "write", actor=actor)
    )
    assert list(shared.attach(other_action).values_list("projected", flat=True)) == list(
        other_action.values_list("projected", flat=True)
    )
    other = SubjectRef.of("auth/user", "other-mixed-scope")
    mismatch = shared.attach(
        Folder._base_manager.annotate(projected=permission_expression(Folder, "read", actor=other))
    )
    assert list(mismatch.order_by("pk").values_list("projected", flat=True)) == [False, False]


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


@pytest.mark.pg_delta
def test_shared_scope_attach_preserves_input_and_sibling_statements(shared_scope_rows):
    _local, actor, allowed, denied = shared_scope_rows
    inner = (
        Folder.objects.with_actor(actor)
        .scoped_for_aggregate()
        .filter(pk=models.OuterRef("pk"))
        .values("pk")
    )
    base = Folder._base_manager.annotate(guarded=models.Exists(inner)).order_by("pk")
    original = list(base.values_list("pk", "guarded"))
    first = Folder.objects.with_actor(actor).scope_cte().attach(base)
    second = Folder.objects.with_actor(actor).scope_cte().attach(base.filter(pk__gt=0))
    assert list(base.values_list("pk", "guarded")) == original
    assert list(first.values_list("pk", "guarded")) == original
    assert list(second.values_list("pk", "guarded")) == original
    assert original == [(allowed.pk, True), (denied.pk, False)]


@pytest.mark.pg_delta
def test_shared_scope_rechecks_caveats_written_after_handle_creation():
    local = install_schema("""
        caveat flag(enabled bool) { enabled }
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user with flag
            permission read = viewer
        }
    """)
    actor = SubjectRef.of("auth/user", "late-caveat")
    with sudo(reason="late caveat fixture"):
        folder = Folder.objects.create(name="caveated")
    handle = Folder.objects.with_actor(actor).scope_cte()
    local.write_relationships(
        [
            RelationshipTuple(
                to_object_ref(folder),
                "viewer",
                actor,
                caveat_name="flag",
                caveat_context={"enabled": True},
            )
        ]
    )
    projected = Folder._base_manager.annotate(
        can_read=permission_expression(Folder, "read", actor=actor)
    )
    assert (
        list(handle.attach(projected).values_list("pk", "can_read"))
        == list(projected.values_list("pk", "can_read"))
        == [(folder.pk, True)]
    )
    scoped = Folder.objects.with_actor(actor)
    assert (
        list(handle.attach(scoped).values_list("pk", flat=True))
        == list(scoped.values_list("pk", flat=True))
        == [folder.pk]
    )


@pytest.mark.pg_delta
def test_shared_scope_rechecks_policy_revision_after_handle_creation(shared_scope_rows):
    _local, actor, allowed, denied = shared_scope_rows
    handle = Folder.objects.with_actor(actor).scope_cte()
    revised = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            permission read = viewer
        }
    """)
    revised.write_relationships([RelationshipTuple(to_object_ref(denied), "viewer", actor)])
    projected = Folder._base_manager.annotate(
        can_read=permission_expression(Folder, "read", actor=actor)
    ).order_by("pk")
    assert (
        list(handle.attach(projected).values_list("pk", "can_read"))
        == list(projected.values_list("pk", "can_read"))
        == [(allowed.pk, False), (denied.pk, True)]
    )


@pytest.mark.pg_delta
def test_shared_scope_reuse_after_nested_resolution_gets_fresh_body(shared_scope_rows):
    local, actor, allowed, denied = shared_scope_rows
    with sudo(reason="nested shared scope fixture"):
        Post.objects.create(title="nested", folder=allowed)
    handle = Folder.objects.with_actor(actor).scope_cte()
    inner = handle.attach(Folder._base_manager.filter(pk__in=handle.queryset().values("pk")))
    list(Post._base_manager.filter(folder__in=inner.values("pk")))
    local.write_relationships([RelationshipTuple(to_object_ref(denied), "viewer", actor)])
    attached = handle.attach(Folder._base_manager.filter(pk__in=handle.queryset().values("pk")))
    assert (
        sorted(attached.values_list("pk", flat=True))
        == sorted(Folder.objects.with_actor(actor).values_list("pk", flat=True))
        == [allowed.pk, denied.pk]
    )


@pytest.mark.pg_delta
def test_nested_shared_scope_attachment_defines_cte_once(shared_scope_rows):
    _local, actor, allowed, _denied = shared_scope_rows
    with sudo(reason="nested attachment fixture"):
        post = Post.objects.create(title="nested", folder=allowed)
    handle = Folder.objects.with_actor(actor).scope_cte()
    inner = handle.attach(Folder._base_manager.filter(pk__in=handle.queryset().values("pk")))
    outer = handle.attach(
        Post._base_manager.filter(folder__in=inner.values("pk")).annotate(
            readable=models.Exists(handle.queryset().filter(pk=models.OuterRef("folder_id")))
        )
    )
    assert len(re.findall(r'"rebac_scope_[^\"]+" AS \(', str(outer.query))) == 1
    assert list(outer.values_list("pk", "readable")) == [(post.pk, True)]


@pytest.mark.pg_delta
def test_shared_scope_union_owner_and_refusals(shared_scope_rows):
    _local, actor, allowed, _denied = shared_scope_rows
    handle = Folder.objects.with_actor(actor).scope_cte()
    arm = Folder._base_manager.filter(pk__in=handle.queryset().values("pk")).values("pk")
    attached = handle.attach(arm.union(arm, all=True))
    inline_arm = Folder._base_manager.filter(
        pk__in=Folder.objects.with_actor(actor).scoped_for_aggregate().values("pk")
    ).values("pk")
    assert (
        list(attached.values_list("pk", flat=True))
        == list(inline_arm.union(inline_arm, all=True).values_list("pk", flat=True))
        == [allowed.pk, allowed.pk]
    )
    with pytest.raises(ValueError, match="unfiltered"):
        Folder.objects.with_actor(actor).filter(name="allowed").scope_cte()
    with pytest.raises(ValueError, match="bounded identity"):
        Folder.objects.with_actor(actor).filter(pk__in=range(257)).scope_cte()
    with pytest.raises(ValueError, match="distinct"):
        Folder.objects.with_actor(actor).scope_cte(actions=("read", "read"))
    with pytest.raises(ValueError, match="unsliced"):
        Folder.objects.with_actor(actor)[:1].scope_cte()
    with pytest.raises(ValueError, match="actor scope"):
        Folder.objects.sudo(reason="shared scope refusal").scope_cte()
    with pytest.raises(ValueError, match="same database alias"):
        handle.attach(Folder._base_manager.using("selected"))


@pytest.mark.pg_delta
def test_shared_scope_keeps_point_and_literal_id_set_paths(shared_scope_rows):
    _local, actor, allowed, denied = shared_scope_rows
    handle = Folder.objects.with_actor(actor).scope_cte()
    for filter_args in ({"pk": allowed.pk}, {"pk__in": [allowed.pk, denied.pk]}):
        inline = Folder.objects.with_actor(actor).filter(**filter_args).scoped()
        attached = handle.attach(inline)
        sql = str(attached.query)
        [name] = re.findall(r'"(rebac_scope_[^\"]+)" AS \(', sql)
        assert sql.count(f'"{name}"') == 1
        assert (
            list(attached.values_list("pk", flat=True))
            == list(inline.values_list("pk", flat=True))
            == [allowed.pk]
        )


@pytest.mark.pg_delta
def test_identity_filtered_scope_cte_keeps_only_candidate_rows(shared_scope_rows):
    _local, actor, allowed, denied = shared_scope_rows
    handle = Folder.objects.with_actor(actor).filter(pk__in=[allowed.pk, denied.pk]).scope_cte()
    rows = handle.attach(
        Folder._base_manager.filter(pk__in=handle.queryset().values("pk")).order_by("pk")
    )
    assert list(rows.values_list("pk", flat=True)) == [allowed.pk]


@pytest.mark.pg_delta
@pytest.mark.parametrize("count", [1, 5, 50])
def test_bounded_multi_action_projection_shares_one_closure(count):
    from rebac import decision_scope

    local = install_schema("""
        definition auth/user {}
        definition auth/group {
            relation member: auth/user | auth/group#member
        }
        definition blog/folder {
            relation owner: auth/user
            relation viewer: auth/user | auth/group#member
            relation editor: auth/user | auth/group#member
            relation banned: auth/user
            permission read = owner + viewer + editor
            permission write = owner + editor
            permission share = (owner + editor) - banned
            permission delete = owner - banned
        }
    """)
    actor = SubjectRef.of("auth/user", "four-actions")
    with sudo(reason="four-action fixture"):
        folders = [Folder.objects.create(name=f"four-{n}") for n in range(count)]
    group = ObjectRef("auth/group", "four-actions")
    relationships = [RelationshipTuple(group, "member", actor)]
    for index, folder in enumerate(folders):
        relation = "owner" if index % 3 == 0 else "editor" if index % 3 == 1 else "viewer"
        subject = actor if relation == "owner" else SubjectRef(group, "member")
        relationships.append(RelationshipTuple(to_object_ref(folder), relation, subject))
        if index % 4 == 0:
            relationships.append(RelationshipTuple(to_object_ref(folder), "banned", actor))
    local.write_relationships(relationships)
    ids = [folder.pk for folder in folders]
    actions = ("read", "write", "share", "delete")

    def projections():
        return (
            Folder._base_manager.filter(pk__in=ids)
            .order_by("pk")
            .annotate(
                **{
                    f"can_{action}": permission_expression(Folder, action, actor=actor)
                    for action in actions
                }
            )
        )

    inline = projections()
    expected = list(inline.values_list("pk", *(f"can_{action}" for action in actions)))
    handle = Folder.objects.with_actor(actor).filter(pk__in=ids).scope_cte(actions=actions)
    shared = handle.attach(projections())
    sql = str(shared.query)
    assert len(re.findall(r'"rebac_scope_[^\"]+_closure" AS \(', sql)) == 1
    with CaptureQueriesContext(connection) as executed:
        actual = list(shared.values_list("pk", *(f"can_{action}" for action in actions)))
    assert len(executed) == 1
    assert actual == expected
    if count == 5:
        scoped_outer = (
            Folder.objects.with_actor(actor)
            .filter(pk__in=ids)
            .order_by("pk")
            .annotate(
                **{
                    f"can_{action}": permission_expression(Folder, action, actor=actor)
                    for action in actions
                }
            )
        )
        scoped_shared = handle.attach(scoped_outer)
        assert (
            list(scoped_shared.values_list("pk", *(f"can_{action}" for action in actions)))
            == expected
        )
        assert len(str(scoped_shared.query).encode()) - len(sql.encode()) < 1_000
    again = handle.attach(shared)
    assert len(re.findall(r'"rebac_scope_[^\"]+_closure" AS \(', str(again.query))) == 1
    assert list(again.values_list("pk", *(f"can_{action}" for action in actions))) == expected
    assert (
        list(
            handle.attach(projections()).values_list("pk", *(f"can_{action}" for action in actions))
        )
        == expected
    )
    with decision_scope():
        assert (
            list(
                handle.attach(projections()).values_list(
                    "pk", *(f"can_{action}" for action in actions)
                )
            )
            == expected
        )
        if count == 5:
            decided = {
                action: set(
                    Folder.objects.with_actor(actor)
                    .with_action(action)
                    .filter(pk__in=ids)
                    .values_list("pk", flat=True)
                )
                for action in actions
            }
            assert all(
                verdict == (pk in decided[action])
                for pk, *verdicts in expected
                for action, verdict in zip(actions, verdicts, strict=True)
            )
    if count == 50:
        assert len(sql.encode()) < len(str(inline.query).encode())
        small_ids = ids[:5]
        small = Folder.objects.with_actor(actor).filter(pk__in=small_ids).scope_cte(actions=actions)
        small_rows = Folder._base_manager.filter(pk__in=small_ids).annotate(
            **{
                f"can_{action}": permission_expression(Folder, action, actor=actor)
                for action in actions
            }
        )
        assert 0 < len(sql.encode()) - len(str(small.attach(small_rows).query).encode()) < 2_000


@pytest.mark.pg_delta
@pytest.mark.parametrize(
    ("first", "second"),
    [("read", "owner"), ("owner", "read"), ("none", "read"), ("undefined", "read")],
)
def test_multi_action_relation_and_false_first_match_inline(first, second):
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation owner: auth/user
            permission read = owner
            permission none = nil
        }
    """)
    actor = SubjectRef.of("auth/user", "relation-action")
    with sudo(reason="relation action fixture"):
        allowed = Folder.objects.create(name="relation-allowed")
        denied = Folder.objects.create(name="relation-denied")
    local.write_relationships([RelationshipTuple(to_object_ref(allowed), "owner", actor)])
    ids = [allowed.pk, denied.pk]
    actions = (first, second)
    inline = (
        Folder._base_manager.filter(pk__in=ids)
        .order_by("pk")
        .annotate(
            **{
                f"can_{action}": permission_expression(Folder, action, actor=actor)
                for action in actions
            }
        )
    )
    handle = (
        Folder.objects.with_actor(actor)
        .with_action(first)
        .filter(pk__in=ids)
        .scope_cte(actions=actions)
    )
    shared = handle.attach(inline)
    fields = ("pk", *(f"can_{action}" for action in actions))
    assert "_closure" in str(shared.query)
    assert list(shared.values_list(*fields)) == list(inline.values_list(*fields))


@pytest.mark.pg_delta
def test_multi_action_with_no_surviving_arm_keeps_outer_rows():
    install_schema("""
        definition auth/user {}
        definition auth/apikey {}
        definition blog/folder {
            relation owner: auth/user
            permission read = owner
            permission write = owner
        }
    """)
    actor = SubjectRef.of("auth/apikey", "unadmitted")
    with sudo(reason="unadmitted actor fixture"):
        folders = [Folder.objects.create(name=f"unadmitted-{n}") for n in range(3)]
    ids = [folder.pk for folder in folders]
    actions = ("read", "write")
    inline = (
        Folder._base_manager.filter(pk__in=ids)
        .order_by("pk")
        .annotate(
            **{
                f"can_{action}": permission_expression(Folder, action, actor=actor)
                for action in actions
            }
        )
    )
    handle = Folder.objects.with_actor(actor).filter(pk__in=ids).scope_cte(actions=actions)
    shared = handle.attach(inline)
    assert "_closure" not in str(shared.query)
    assert list(shared.values_list("pk", "can_read", "can_write")) == [
        (folder.pk, False, False) for folder in folders
    ]
    assert list(shared.values_list("pk", "can_read", "can_write")) == list(
        inline.values_list("pk", "can_read", "can_write")
    )


@pytest.mark.pg_delta
@pytest.mark.parametrize("actions", [(), ("read", "preview"), ("read", "write")])
def test_bounded_scope_cte_uses_non_pk_integer_identity(actions):
    local = install_schema("""
        caveat flag(enabled bool) { enabled }
        definition auth/user {}
        definition blog/integeridentityfolder {
            relation viewer: auth/user
            relation editor: auth/user
            relation gated: auth/user with flag
            permission read = viewer + editor
            permission write = editor
            permission preview = gated
        }
    """)
    actor = SubjectRef.of("auth/user", "integer-identity")
    with sudo(reason="integer identity fixture"):
        folders = [
            IntegerIdentityFolder.objects.create(external_id=101 + n, name=f"ext-{n}")
            for n in range(3)
        ]
    relationships = [RelationshipTuple(to_object_ref(folders[0]), "viewer", actor)]
    if "preview" in actions:
        relationships.append(
            RelationshipTuple(
                to_object_ref(folders[2]),
                "gated",
                actor,
                caveat_name="flag",
                caveat_context={"enabled": True},
            )
        )
    local.write_relationships(relationships)
    candidates = [folders[0].external_id, folders[1].external_id]
    handle = (
        IntegerIdentityFolder.objects.with_actor(actor)
        .filter(external_id__in=candidates)
        .scope_cte(actions=actions)
    )
    expected = list(
        IntegerIdentityFolder.objects.with_actor(actor)
        .filter(external_id__in=candidates)
        .values_list("pk", flat=True)
    )
    actual = list(
        handle.attach(
            IntegerIdentityFolder._base_manager.filter(
                pk__in=handle.queryset().values("pk")
            ).order_by("pk")
        ).values_list("pk", flat=True)
    )
    assert actual == expected == [folders[0].pk]


@pytest.mark.pg_delta
def test_scope_cte_candidate_limit_and_empty_set_are_explicit():
    actor = SubjectRef.of("auth/user", "bounded-limit")
    assert SCOPE_CTE_CANDIDATE_LIMIT == 256
    with pytest.raises(ValueError, match="bounded identity"):
        Folder.objects.with_actor(actor).filter(pk__in=[]).scope_cte()
    with pytest.raises(ValueError, match="bounded identity"):
        Folder.objects.with_actor(actor).filter(
            pk__in=list(range(1, SCOPE_CTE_CANDIDATE_LIMIT + 2))
        ).scope_cte()


@pytest.mark.pg_delta
def test_multi_action_caveat_verdict_falls_back_to_inline():
    local = install_schema("""
        caveat flag(enabled bool) { enabled }
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user with flag
            relation banned: auth/user
            permission read = viewer
            permission write = viewer - banned
        }
    """)
    actor = SubjectRef.of("auth/user", "caveated-four")
    with sudo(reason="multi-action caveat fixture"):
        folder = Folder.objects.create(name="caveated-four")
    local.write_relationships(
        [
            RelationshipTuple(
                to_object_ref(folder),
                "viewer",
                actor,
                caveat_name="flag",
                caveat_context={"enabled": True},
            ),
        ]
    )
    actions = ("read", "write")
    handle = Folder.objects.with_actor(actor).filter(pk=folder.pk).scope_cte(actions=actions)
    inline = Folder._base_manager.annotate(
        can_read=permission_expression(Folder, "read", actor=actor),
        can_write=permission_expression(Folder, "write", actor=actor),
    )
    shared = handle.attach(inline)
    assert "_closure" not in str(shared.query)
    assert (
        list(shared.values_list("pk", "can_read", "can_write"))
        == list(inline.values_list("pk", "can_read", "can_write"))
        == [(folder.pk, True, True)]
    )
    local.write_relationships([RelationshipTuple(to_object_ref(folder), "banned", actor)])
    assert list(handle.attach(inline).values_list("pk", "can_read", "can_write")) == [
        (folder.pk, True, False)
    ]


@pytest.mark.pg_delta
def test_bounded_projection_does_not_narrow_broader_outer_query(shared_scope_rows):
    local, actor, allowed, denied = shared_scope_rows
    local.write_relationships([RelationshipTuple(to_object_ref(denied), "viewer", actor)])
    handle = Folder.objects.with_actor(actor).filter(pk=allowed.pk).scope_cte(actions=("read",))
    outer = Folder._base_manager.annotate(
        can_read=permission_expression(Folder, "read", actor=actor)
    ).order_by("pk")
    assert (
        list(handle.attach(outer).values_list("pk", "can_read"))
        == list(outer.values_list("pk", "can_read"))
        == [(allowed.pk, True), (denied.pk, True)]
    )


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


@pytest.mark.pg_delta
def test_bounded_multi_action_arrow_projections_match_inline(shared_arrow_rows):
    _local, actor, post = shared_arrow_rows
    actions = ("once", "many")
    inline = Post._base_manager.filter(pk=post.pk).annotate(
        can_once=permission_expression(Post, "once", actor=actor),
        can_many=permission_expression(Post, "many", actor=actor),
    )
    handle = (
        Post.objects.with_actor(actor)
        .with_action("once")
        .filter(pk=post.pk)
        .scope_cte(actions=actions)
    )
    shared = handle.attach(inline)
    assert "_closure" in str(shared.query)
    assert (
        list(shared.values_list("pk", "can_once", "can_many"))
        == list(inline.values_list("pk", "can_once", "can_many"))
        == [(post.pk, True, True)]
    )


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


def test_three_operand_union_prepares_once_and_is_smaller_than_inline(monkeypatch):
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
    assert decided_bytes <= inline_bytes


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
