"""Frozen id sets retain Django's compact subquery aliases."""

import re

import pytest
from django.core.exceptions import FieldError
from django.db import connection
from django.db.models import Exists, OuterRef, QuerySet
from django_cte import CTE, with_cte

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo
from rebac.compile import ctes, read
from rebac.compile.predicate import Compiler
from rebac.schema import parse_zed
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_recursive_role_aliases_match_the_nested_shape(settings, monkeypatch, storage):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    settings.REBAC_DEPTH_LIMIT = 16
    original = Compiler._ids

    def nested(self, rows):
        # Reproduce 0.31's closure embedding, holding the rest of the
        # compiler constant. 0.32 freezes this same closure as an id set.
        if any(c.name == "rebac_reached" for c in getattr(rows.query, "_with_ctes", ())):
            return rows
        return original(self, rows)

    local = install_schema("""
        definition auth/user {}
        definition purchase/role {
            relation direct: auth/user
            relation includes: purchase/role
            permission member = direct + includes->member
        }
        definition blog/post {
            relation purchaser: purchase/role // rebac:const=purchaser_all
            relation viewer: auth/user
            permission read = purchaser->member + viewer
        }
    """)
    actor = SubjectRef.of("auth/user", "alice")
    with sudo(reason="compact recursive role fixture"):
        post = Post.objects.create(title="visible")
    local.write_relationships(
        [
            RelationshipTuple(
                ObjectRef("purchase/role", "purchaser_all"),
                "includes",
                SubjectRef.of("purchase/role", "child"),
            ),
            RelationshipTuple(ObjectRef("purchase/role", "child"), "direct", actor),
        ]
    )
    fragments = {}
    for shape, lower in (("shared", original), ("nested", nested)):
        with monkeypatch.context() as patch:
            patch.setattr(Compiler, "_ids", lower)
            # Each shape must compile afresh, not reuse the other's kept SQL.
            read.reset()
            rows = Post.objects.with_actor(actor).scoped().order_by().values("pk")
            sql, _ = rows.query.sql_with_params()
            # This fixture's helper has no nested SELECT: the following SELECT
            # is the wrapper's projection. Include the definition's name/parens.
            matched = re.search(r'"rebac_reached" AS \((.*?)\) SELECT ', sql)
            assert matched is not None
            fragment = matched[0][: -len(" SELECT ")]
            fragments[shape] = fragment.encode()
            table = "rebac_relationshipregistry" if storage == "registry" else "rebac_relationship"
            assert fragment.count(f'FROM "{table}" "U0"') == 2
            recursive_alias = "U3" if storage == "registry" else "U1"
            assert f'INNER JOIN "rebac_reached" "{recursive_alias}"' in fragment
            assert f'"{recursive_alias}"."_rebac_depth"' in fragment
            for name in (table, "rebac_rebacresource", "rebac_reached"):
                assert f'"{name}".' not in fragment
            if storage == "registry":
                for alias in ("U1", "U2"):
                    assert fragment.count(f'INNER JOIN "rebac_rebacresource" "{alias}"') == 2
            assert list(rows.values_list("pk", flat=True)) == [post.pk]
            assert not Post.objects.with_actor(SubjectRef.of("auth/user", "outsider")).exists()
    # Pin the byte-identity claim at the helper itself. Changes elsewhere in
    # the read must not trip an arbitrary full-statement byte ceiling.
    assert fragments["shared"] == fragments["nested"]


def test_freezing_preserves_recursive_sql_and_parameters():
    def reached(cte):
        first = Folder._base_manager.filter(name="seed").order_by().values("pk")
        step = cte.join(Folder._base_manager.all(), parent_id=cte.col.pk).order_by().values("pk")
        return first.union(step)

    cte = CTE.recursive(reached, name="local_folders")
    rows = with_cte(cte, select=QuerySet(model=Folder, query=cte.queryset().query))
    before = rows.query.sql_with_params()
    compact = ctes.id_sql(rows.query, connection.alias)
    assert compact[1] == before[1] == ("seed",)
    assert len(compact[0]) < len(before[0])
    assert compact == ctes.id_sql(rows.query, connection.alias)
    assert rows.query.sql_with_params() == before


@pytest.mark.parametrize("levels", [1, 2])
def test_id_set_rejects_a_reference_outside_its_body(levels):
    if levels == 1:
        body = Folder._base_manager.filter(name=OuterRef("title")).values("pk")
    else:
        inner = Post._base_manager.filter(title=OuterRef(OuterRef("title")))
        body = Folder._base_manager.filter(Exists(inner)).values("pk")
    compiler = Compiler(
        parse_zed("definition auth/user {}"), SubjectRef.of("auth/user", "alice"), connection.alias
    )
    # _ids requires an uncorrelated body. Its empty resolution scope must
    # reject references escaping that body, including through another subquery.
    with pytest.raises(FieldError) as error:
        compiler._ids(body)
    assert str(error.value) == "Cannot resolve keyword 'title' into field. Choices are: "
    assert compiler._id_sets == []


def _frozen_ids(rows):
    # Execute only Django-produced SQL, exactly as a frozen id-set body does.
    sql, params = ctes.id_sql(rows.query, connection.alias)
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return sorted(row[0] for row in cursor.fetchall())


@pytest.mark.parametrize("membership", ["exists", "in"])
def test_frozen_body_keeps_its_nested_subquery_correlations(membership):
    with sudo(reason="nested frozen subquery fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        Folder.objects.create(name="other", parent=root)
        Post.objects.create(title="match", folder=child)
        Post.objects.create(title="miss", folder=root)
    inner = Post._base_manager.filter(folder=OuterRef("pk"), title="match")
    deeper = Post._base_manager.filter(
        Exists(Folder._base_manager.filter(pk=OuterRef("folder_id"), parent__name="root"))
    ).values("folder_id")
    body = Folder._base_manager.filter(parent__name="root").filter(pk__in=deeper)
    if membership == "exists":
        body = body.filter(Exists(inner))
    else:
        body = body.filter(pk__in=inner.values("folder_id"))
    body = body.order_by().values("pk")
    expected = sorted(body.values_list("pk", flat=True))
    assert expected == [child.pk]
    assert _frozen_ids(body) == expected


def test_frozen_body_keeps_two_level_outerref_on_the_same_table():
    with sudo(reason="two-level frozen subquery fixture"):
        root = Folder.objects.create(name="root")
        Folder.objects.create(name="other")
        Post.objects.create(title="root")
    deep = Post._base_manager.filter(title=OuterRef(OuterRef("name")))
    body = (
        Folder._base_manager.filter(Exists(Folder._base_manager.filter(Exists(deep))))
        .order_by()
        .values("pk")
    )
    # Use Django's ordinary subquery path as the unfrozen comparison. Without
    # alias resolution, both Folder levels can use the same table qualifier
    # and the two-level reference captures the inner Folder instead.
    expected = sorted(Folder._base_manager.filter(pk__in=body).values_list("pk", flat=True))
    assert expected == [root.pk]
    assert _frozen_ids(body) == expected


def test_frozen_recursive_union_preserves_parameter_order_and_rows():
    with sudo(reason="recursive parameter order fixture"):
        root = Folder.objects.create(name="seed", kind="start")
        child = Folder.objects.create(name="child", kind="target", parent=root)
        blocked = Folder.objects.create(name="blocked", kind="target", parent=root)
        leaf = Folder.objects.create(name="leaf", kind="target", parent=child)
        for folder, body in ((child, "allow"), (blocked, "allow"), (leaf, "deny")):
            Post.objects.create(title="link", body=body, folder=folder)

    def reached(cte):
        first = (
            Folder._base_manager.filter(name="seed").filter(kind="start").order_by().values("pk")
        )
        inner = (
            Post._base_manager.filter(title="link")
            .filter(body="allow")
            .filter(folder=OuterRef("pk"))
        )
        step = (
            cte.join(Folder._base_manager.all(), parent_id=cte.col.pk)
            .filter(Exists(inner))
            .exclude(name="blocked")
            .order_by()
            .values("pk")
        )
        return first.union(step)

    cte = CTE.recursive(reached, name="local_folders")
    rows = with_cte(cte, select=QuerySet(model=Folder, query=cte.queryset().query)).filter(
        pk__in=Folder._base_manager.filter(kind="target").values("pk")
    )
    before = rows.query.sql_with_params()
    compact = ctes.id_sql(rows.query, connection.alias)
    # EXISTS adds its SELECT 1 parameter between the seed and step filters.
    assert compact[1] == before[1] == ("seed", "start", 1, "link", "allow", "blocked", "target")
    expected = sorted(rows.values_list("pk", flat=True))
    assert expected == [child.pk]
    assert _frozen_ids(rows) == expected
    assert compact == ctes.id_sql(rows.query, connection.alias)
