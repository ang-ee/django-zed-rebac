"""Frozen id sets retain Django's compact subquery aliases."""

import re

import pytest
from django.db import connection
from django.db.models import QuerySet
from django_cte import CTE, with_cte

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo
from rebac.compile import ctes
from rebac.compile.predicate import Compiler
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


@pytest.mark.parametrize("shape", ["nested", "shared"])
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_recursive_role_aliases_and_sql_budget(settings, monkeypatch, shape, storage):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    settings.REBAC_DEPTH_LIMIT = 16
    if shape == "nested":
        original = Compiler._ids

        def nested(self, rows):
            # Reproduce 0.31's closure embedding, holding the rest of the
            # compiler constant. 0.32 freezes this same closure as an id set.
            if any(c.name == "rebac_reached" for c in getattr(rows.query, "_with_ctes", ())):
                return rows
            return original(self, rows)

        monkeypatch.setattr(Compiler, "_ids", nested)
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
    rows = Post.objects.with_actor(actor).scoped().order_by().values("pk")
    sql, _ = rows.query.sql_with_params()
    # This fixture's helper has no nested SELECT: the following SELECT is
    # the wrapper's projection. Include the definition's name and parentheses.
    matched = re.search(r'"rebac_reached" AS \((.*?)\) SELECT ', sql)
    assert matched is not None
    fragment = matched[0][: -len(" SELECT ")]
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

    # Byte ceilings cover SQLite and PostgreSQL (UNION parentheses differ).
    # Before the fix the shared registry helper was 1,654 bytes with %s
    # placeholders, and its full SQLite read was 7,347 bytes.
    assert len(fragment.encode()) <= (1208 if storage == "registry" else 880)
    budgets = {
        ("denormalized", "nested"): 4900,
        ("denormalized", "shared"): 5000,
        ("registry", "nested"): 5900,
        ("registry", "shared"): 6000,
    }
    assert len(sql.encode()) <= budgets[storage, shape]
    assert list(rows.values_list("pk", flat=True)) == [post.pk]
    assert not Post.objects.with_actor(SubjectRef.of("auth/user", "outsider")).exists()


def test_freezing_clones_recursive_queries_and_preserves_parameters():
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
