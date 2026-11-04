"""Shared identity plans must not inherit model default ordering."""

import re

import pytest
from django.db import connection, models
from django.db.models import Exists, OuterRef, Prefetch, Subquery
from django.test.utils import CaptureQueriesContext

from rebac import RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.expressions import permission_expression
from rebac.testing import install_schema
from tests.testapp.models import OrderedFolder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


@pytest.fixture
def ordered_rows():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation owner: auth/user
            relation viewer: auth/user
            permission read = owner + viewer
            permission write = owner
            permission share = viewer
            permission delete = owner
        }
    """)
    actor = SubjectRef.of("auth/user", "ordered-plan")
    with sudo(reason="ordered plan fixture"):
        owner = OrderedFolder.objects.create(name="owner", kind="z")
        viewer = OrderedFolder.objects.create(name="viewer", kind="a")
        denied = OrderedFolder.objects.create(name="denied", kind="m")
        posts = [
            Post.objects.create(title=f"ordered-{i}", folder=folder)
            for i, folder in enumerate((owner, viewer, denied))
        ]
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(owner), "owner", actor),
            RelationshipTuple(to_object_ref(viewer), "viewer", actor),
        ]
    )
    return actor, (owner, viewer, denied), posts


def _projected(actor, actions):
    return OrderedFolder._base_manager.annotate(
        **{
            f"can_{action}": permission_expression(OrderedFolder, action, actor=actor)
            for action in actions
        }
    )


def _assert_internal_ordering(sql, *, outer_ordered=True):
    assert '_closure" AS (' in sql or "rebac_scope_" in sql
    orders = re.findall(r"\bORDER BY\b", sql)
    assert len(orders) == int(outer_ordered), sql
    if outer_ordered:
        assert re.search(
            r'ORDER BY "testapp_folder"\."kind" ASC, "testapp_folder"\."id" DESC$', sql
        )


@pytest.mark.parametrize("actions", [("read",), ("read", "write", "share", "delete")])
@pytest.mark.parametrize("bounded", [False, True])
def test_ordered_handle_matches_inline_and_keeps_outer_order(ordered_rows, actions, bounded):
    actor, folders, _posts = ordered_rows
    ids = [folder.pk for folder in folders]
    outer = _projected(actor, actions).filter(pk__in=ids)
    source = OrderedFolder.objects.with_actor(actor)
    if bounded:
        source = source.filter(pk__in=ids)
    handle = source.scope_cte(actions=actions)
    attached = handle.attach(outer)
    fields = ("pk", *(f"can_{action}" for action in actions))
    assert list(attached.values_list(*fields)) == list(outer.values_list(*fields))
    assert attached.query.order_by == outer.query.order_by
    sql = str(attached.query)
    _assert_internal_ordering(sql)
    if len(actions) > 1:
        assert len(re.findall(r'"rebac_scope_[^"]+_closure" AS \(', sql)) == 1


def test_ordered_late_prefetch_matches_inline(ordered_rows):
    actor, _folders, posts = ordered_rows
    actions = ("read", "write", "share", "delete")
    roots = Post._base_manager.filter(pk__in=[post.pk for post in posts]).order_by("pk")
    related = OrderedFolder.objects.with_actor(actor).annotate(
        **{
            f"can_{action}": permission_expression(OrderedFolder, action, actor=actor)
            for action in actions
        }
    )

    def fetch(queryset):
        with CaptureQueriesContext(connection) as captured:
            page = list(roots.prefetch_related(Prefetch("folder", queryset=queryset)))
        values = [
            (
                post.folder_id,
                *(
                    getattr(post._state.fields_cache.get("folder"), f"can_{action}", None)
                    for action in actions
                ),
            )
            for post in page
        ]
        return values, captured.captured_queries[-1]["sql"]

    expected, _ = fetch(related)
    handle = OrderedFolder.objects.with_actor(actor).scope_cte(actions=actions)
    actual, sql = fetch(handle.attach(related))
    assert actual == expected
    assert len(re.findall(r'"rebac_scope_[^"]+_closure" AS \(', sql)) == 1
    _assert_internal_ordering(sql, outer_ordered=False)


@pytest.mark.parametrize("shape", ["exists", "subquery", "union", "aggregate"])
def test_ordered_identity_reference_shapes(ordered_rows, shape):
    actor, folders, _posts = ordered_rows
    handle = OrderedFolder.objects.with_actor(actor).scope_cte(actions=("read", "write"))
    ids = [folder.pk for folder in folders]
    base = OrderedFolder._base_manager.filter(pk__in=ids)
    if shape == "exists":
        outer = base.annotate(admitted=Exists(handle.queryset().filter(pk=OuterRef("pk"))))
        fields = ("pk", "admitted")
        expected = [(folders[1].pk, True), (folders[2].pk, False), (folders[0].pk, True)]
    elif shape == "subquery":
        outer = base.annotate(
            admitted_id=Subquery(handle.queryset().filter(pk=OuterRef("pk")).values("pk")[:1])
        )
        fields = ("pk", "admitted_id")
        expected = [
            (folders[1].pk, folders[1].pk),
            (folders[2].pk, None),
            (folders[0].pk, folders[0].pk),
        ]
    elif shape == "union":
        first = base.filter(pk=folders[0].pk).order_by().values("pk")
        second = base.filter(pk__in=handle.queryset().values("pk")).order_by().values("pk")
        outer = first.union(second).order_by("-pk")
        fields = ("pk",)
        expected = [(folders[1].pk,), (folders[0].pk,)]
    else:
        outer = base.filter(pk__in=handle.queryset().values("pk"))
        fields = ()
        expected = {"total": 2}
    attached = handle.attach(outer)
    sql = str(attached.query)
    if shape == "aggregate":
        assert attached.aggregate(total=models.Count("pk")) == expected
    else:
        assert list(attached.values_list(*fields)) == expected
    assert len(re.findall(r'"rebac_scope_[^"]+_closure" AS \(', sql)) == 1
    if shape == "union":
        assert len(re.findall(r"\bORDER BY\b", sql)) == 1
    else:
        _assert_internal_ordering(sql)
