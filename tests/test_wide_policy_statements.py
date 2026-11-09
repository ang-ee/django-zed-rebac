"""A wide policy compiles to compact statements that SQLite accepts.

An application console's policy has about 150 types, more than a thousand
arrows and permissions that refer to each other, so one scope reaches the same
id set (an arrow's targets, a subject set's members) from many arms. Each
distinct id set is defined once, as a CTE of the statement, and selected by
name wherever it occurs: a statement grows with the number of distinct id
sets, not with the number of paths to them, and the same for every actor.

Nesting through names also bounds SQLite's expression depth. SQLite adds up
the depth of every subquery nested in a WHERE clause while it resolves names
and refuses a statement deeper than ``SQLITE_LIMIT_EXPR_DEPTH`` (1000); a CTE
body does not count toward the depth of the expression that selects from it,
so the depth grows with the length of an arrow chain, not its square.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from django.db import connection

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.compile import read
from rebac.compile.predicate import Compiler
from rebac.testing import install_schema
from tests.testapp.models import Post

pytestmark = pytest.mark.django_db
ACTOR = SubjectRef.of("auth/user", "wide")
OTHER = SubjectRef.of("auth/user", "other")
# A chain of types, each with subject-set arms and an arrow to the next type.
CHAIN = 24
ARMS = 6
# Every type also has ``manage = view + side0->member``, and the arrow to the
# next type is followed to both ``view`` and ``manage``: each level reaches the
# next level's ``view`` twice, so the number of paths doubles per level. SQLite
# still expands each CTE reference while it resolves the statement, so this
# stays small enough for SQLite to execute.
BRANCHING = 9


def _schema(levels: int, *, branching: bool) -> str:
    lines = [
        "definition auth/user {}",
        "definition auth/group {",
        "    relation member: auth/user | auth/group#member",
        "}",
    ]
    for level in range(levels):
        following = f"chain/t{level + 1}" if level + 1 < levels else None
        lines.append(f"definition chain/t{level} {{")
        lines.append("    relation viewer: auth/user | auth/group#member")
        arms = ["viewer"]
        for arm in range(ARMS):
            lines.append(f"    relation side{arm}: auth/group")
            arms.append(f"side{arm}->member")
        if following is not None:
            lines.append(f"    relation up: {following}")
            arms.append("up->view")
            if branching:
                arms.append("up->manage")
        lines.append(f"    permission view = {' + '.join(arms)}")
        lines.append("    permission manage = view + side0->member")
        lines.append("}")
    lines += [
        "definition blog/post {",
        "    relation parent: chain/t0",
        "    relation viewer: auth/user",
        "    permission read = viewer + parent->view",
        "}",
    ]
    return "\n".join(lines)


def _install(levels: int, *, branching: bool):
    local = install_schema(_schema(levels, branching=branching))
    with sudo(reason="wide policy fixture"):
        post = Post.objects.create(title="reached at the end of the chain")
    tuples = [RelationshipTuple(to_object_ref(post), "parent", SubjectRef.of("chain/t0", "0"))]
    tuples += [
        RelationshipTuple(
            ObjectRef(f"chain/t{level}", "0"), "up", SubjectRef.of(f"chain/t{level + 1}", "0")
        )
        for level in range(levels - 1)
    ]
    tuples.append(RelationshipTuple(ObjectRef(f"chain/t{levels - 1}", "0"), "viewer", ACTOR))
    local.write_relationships(tuples)
    return local, post


def _scope_sql(local, actor: SubjectRef) -> str:
    scope = read.scope_q(backend=local, model=Post, action="read", actor=actor, using="default")
    sql, _params = Post._base_manager.filter(scope).values("pk").query.sql_with_params()
    return sql


def _scope_statement(local, actor: SubjectRef) -> tuple[str, tuple[object, ...]]:
    """The compiled scope statement, before its parameters are bound."""
    operation = read._Operation.begin(local, actor, "default")
    compiled = read._scope_statement(operation, Post, ("blog/post", "read"))
    assert not isinstance(compiled, bool)
    return compiled.sql, tuple(compiled.params)


def _id_set_name() -> str:
    """A pattern for an id set's quoted CTE name."""
    opening, closing = connection.ops.quote_name("rebac_ids_0").split("rebac_ids_0")
    return re.escape(opening) + r"(_*rebac_ids_[0-9a-f]{16})" + re.escape(closing)


def _definitions(sql: str, params: tuple[object, ...]) -> list[tuple[str, tuple[object, ...]]]:
    """Each id-set definition of a statement: its body and the parameters it binds."""
    found = []
    for match in re.finditer(_id_set_name() + r" AS \(", sql):
        depth = 0
        for end in range(match.end() - 1, len(sql)):
            depth += {"(": 1, ")": -1}.get(sql[end], 0)
            if depth == 0:
                break
        body = sql[match.end() : end]
        start = sql[: match.end()].count("%s")
        found.append((body, params[start : start + body.count("%s")]))
    return found


def _counting_nodes(monkeypatch) -> list[int]:
    """Count the permission nodes the compiler builds (a memo miss each)."""
    built = [0]
    node = Compiler._node

    def counted(self, *args, **kwargs):
        built[0] += 1
        return node(self, *args, **kwargs)

    monkeypatch.setattr(Compiler, "_node", counted)
    return built


def _scope_ids(local, actor: SubjectRef) -> list[int]:
    scope = read.scope_q(backend=local, model=Post, action="read", actor=actor, using="default")
    return list(Post._base_manager.filter(scope).values_list("pk", flat=True))


def test_a_statement_grows_with_its_distinct_id_sets_not_its_paths(monkeypatch):
    built = _counting_nodes(monkeypatch)
    single = _scope_sql(install_schema(_schema(BRANCHING, branching=False)), ACTOR)
    single_nodes = built[0]
    local, post = _install(BRANCHING, branching=True)
    read.reset()
    built[0] = 0
    sql = _scope_sql(local, ACTOR)
    # 2 ** 9 paths reach the last type, through the same id sets as one path:
    # the compiler builds each permission node once, and writes each id set once.
    assert built[0] <= 2 * single_nodes
    assert len(sql) < 1.5 * len(single)
    assert len(sql) < 120_000
    # No id set is defined twice with the same text and the same parameters.
    statement, params = _scope_statement(local, ACTOR)
    definitions = _definitions(statement, params)
    assert definitions
    assert len(definitions) == len(set(definitions))
    # The kept statement defines every id set it selects from.
    defined = set(re.findall(_id_set_name() + r" AS \(", statement))
    assert set(re.findall(r"FROM " + _id_set_name(), statement)) == defined
    assert _scope_ids(local, ACTOR) == [post.pk]
    # Nothing in the statement depends on the actor beyond its bound
    # parameters: compiled afresh for another actor, it is the same text.
    read.reset()
    assert _scope_sql(local, OTHER) == sql
    assert _scope_ids(local, OTHER) == []


def test_each_enumeration_branch_defines_the_id_sets_it_selects():
    # The branches of an enumeration share one compiler and are not kept.
    local, post = _install(3, branching=True)
    assert local.accessible(subject=ACTOR, action="read", resource_type="blog/post") == [
        str(post.pk)
    ]
    assert local.accessible(subject=OTHER, action="read", resource_type="blog/post") == []


@pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite's expression depth")
def test_a_long_arrow_chain_reads_under_a_fixed_expression_depth():
    local, post = _install(CHAIN, branching=False)
    connection.ensure_connection()
    raw = connection.connection
    previous = raw.getlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH)
    # Measured at 341; well inside SQLite's default of 1000.
    raw.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 450)
    try:
        assert _scope_ids(local, ACTOR) == [post.pk]
        assert local.check_access(
            subject=ACTOR, action="read", resource=to_object_ref(post)
        ).allowed
    finally:
        raw.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, previous)


@pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite's expression depth")
def test_the_same_chain_with_id_sets_nested_in_place_exceeds_sqlites_depth(monkeypatch):
    local, _post = _install(CHAIN, branching=False)
    # Each id set where it occurs, not selected from its CTE.
    monkeypatch.setattr(Compiler, "_ids", lambda self, rows: rows.order_by())
    read.reset()
    scope = read.scope_q(backend=local, model=Post, action="read", actor=ACTOR, using="default")
    with pytest.raises(Exception, match="Expression tree is too large"):
        list(Post._base_manager.filter(scope).values_list("pk", flat=True))
