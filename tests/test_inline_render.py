"""Prepared permission scopes render SQL without database reads."""

from __future__ import annotations

import importlib.util

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models
from django.db.models import Exists, OuterRef, Subquery
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.testing import install_schema
from tests.testapp.models import Attachment, Folder, Post

pytestmark = pytest.mark.django_db
ACTOR = SubjectRef.of("auth/user", "inline-render")
SCHEMAS = {
    "direct": """
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user
            permission read = viewer
        }
    """,
    "arrow": """
        definition auth/user {}
        definition blog/folder {
            relation parent: blog/folder // rebac:field=parent
            relation viewer: auth/user
            permission read = viewer + parent->read
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->read
        }
    """,
    "groups": """
        definition auth/user {}
        definition auth/group {
            relation member: auth/user | auth/group#member
        }
        definition blog/post {
            relation viewer: auth/group#member
            permission read = viewer
        }
    """,
    "constant": """
        definition auth/user {}
        definition site/role {
            relation member: auth/user
        }
        definition blog/post {
            relation admin: site/role // rebac:const=admin
            permission read = admin->member
        }
    """,
    "generic": """
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user
            permission read = viewer
        }
        definition test/attachment {
            relation target: blog/post // rebac:field=target
            permission read = target->read
        }
    """,
    "caveat": """
        caveat active(flag bool) { flag }
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user with active
            permission read = viewer
        }
    """,
}


@pytest.fixture(params=SCHEMAS)
def scoped(request):
    shape = request.param
    local = install_schema(SCHEMAS[shape])
    with sudo(reason="inline render fixture"):
        post = Post.objects.create(title="visible")
        if shape == "arrow":
            folder = Folder.objects.create(name="root")
            Folder.objects.create(name="child", parent=folder)
            post.folder = folder
            post.save(update_fields=["folder"])
        if shape == "generic":
            ct = ContentType.objects.get_for_model(Post)
            row = Attachment.objects.create(content_type=ct, object_id=post.pk)
        else:
            row = post
    if shape == "arrow":
        grants = [RelationshipTuple(to_object_ref(folder), "viewer", ACTOR)]
    elif shape == "groups":
        grants = [
            RelationshipTuple(ObjectRef("auth/group", "inner"), "member", ACTOR),
            RelationshipTuple(
                ObjectRef("auth/group", "outer"),
                "member",
                SubjectRef.of("auth/group", "inner", "member"),
            ),
            RelationshipTuple(
                to_object_ref(post), "viewer", SubjectRef.of("auth/group", "outer", "member")
            ),
        ]
    elif shape == "constant":
        grants = [RelationshipTuple(ObjectRef("site/role", "admin"), "member", ACTOR)]
    elif shape == "caveat":
        grants = [
            RelationshipTuple(
                to_object_ref(post),
                "viewer",
                ACTOR,
                caveat_name="active",
                caveat_context={"flag": True},
            )
        ]
    else:
        grants = [RelationshipTuple(to_object_ref(post), "viewer", ACTOR)]
    local.write_relationships(grants)
    return local, row, type(row).objects.with_actor(ACTOR).scoped()


@pytest.fixture
def direct_scope():
    local = install_schema(SCHEMAS["direct"])
    with sudo(reason="inline render direct fixture"):
        post = Post.objects.create(title="visible")
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", ACTOR)])
    return local, post


@pytest.mark.parametrize("embedding", ["direct", "subquery", "exists", "union"])
def test_sql_render_executes_no_queries(scoped, embedding):
    _local, row, rows = scoped
    plain = models.QuerySet(model=type(row), using="default")
    if embedding == "direct":
        statement = rows
    elif embedding == "subquery":
        statement = plain.annotate(allowed=Subquery(rows.order_by("pk").values("pk")[:1]))
    elif embedding == "exists":
        statement = plain.annotate(allowed=Exists(rows.filter(pk=OuterRef("pk"))))
    else:
        statement = plain.values("pk").union(rows.values("pk"))
    with CaptureQueriesContext(connection) as queries:
        sql = str(statement.query)
    assert len(queries) == 0
    assert "SELECT" in sql
    assert row.pk is not None


def test_lazy_with_actor_embedding_renders_without_queries(scoped):
    _local, row, _rows = scoped
    lazy = type(row).objects.with_actor(ACTOR)
    plain = models.QuerySet(model=type(row), using="default")
    statement = plain.annotate(allowed=Exists(lazy.filter(pk=OuterRef("pk"))))
    with CaptureQueriesContext(connection) as queries:
        assert "SELECT" in str(statement.query)
    assert len(queries) == 0


def test_lazy_with_actor_count_is_one_statement(direct_scope):
    _local, _row = direct_scope
    with evaluator_scope():
        Post.objects.with_actor(ACTOR).scoped()
        with CaptureQueriesContext(connection) as queries:
            count = Post.objects.with_actor(ACTOR).count()
    assert count == 1
    assert len(queries) == 1


def test_tuple_write_inside_evaluator_scope_is_visible_next_statement(direct_scope):
    local, row = direct_scope
    edge = RelationshipTuple(to_object_ref(row), "viewer", ACTOR)
    with evaluator_scope():
        assert Post.objects.with_actor(ACTOR).filter(pk=row.pk).exists()
        local.delete_relationship(edge)
        assert not Post.objects.with_actor(ACTOR).filter(pk=row.pk).exists()


def test_stock_django_import_and_scope(direct_scope):
    assert importlib.util.find_spec("django.db.statements") is None
    import rebac

    _local, row = direct_scope
    assert rebac.backend() is not None
    assert Post.objects.with_actor(ACTOR).filter(pk=row.pk).exists()
