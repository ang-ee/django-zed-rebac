"""A wide policy compiles to statements SQLite accepts.

SQLite adds up the expression depth of every subquery nested in a WHERE
clause while it resolves a statement, and refuses one deeper than
``SQLITE_LIMIT_EXPR_DEPTH`` (1000 by default). Inline compilation nests the
id set of each arrow and subject set inside the next, so a policy as wide as
an application console's (more than a thousand arrows over about 150 types,
chains of arrows twenty types long) reached that limit on every scoped read.
On SQLite each id set is a statement-local CTE, whose body does not count
toward the depth of the expression that selects from it, so the depth grows
with the length of an arrow chain instead of its square.
"""

from __future__ import annotations

import sqlite3

import pytest
from django.db import connection

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Post

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite's expression depth"),
]
ACTOR = SubjectRef.of("auth/user", "wide")
# Types in one arrow chain, and arms (arrows and subject sets) on each type.
CHAIN = 24
ARMS = 6
# The depth the reads must stay under, inside SQLite's default of 1000: with
# every third nested id set a CTE, depth grows with the length of an arrow
# chain (about 25 levels a type here, 610 in all), no longer with its square.
BOUND = 650


def _wide_schema() -> str:
    lines = [
        "definition auth/user {}",
        "definition auth/group {",
        "    relation member: auth/user | auth/group#member",
        "}",
    ]
    for level in range(CHAIN):
        following = f"chain/t{level + 1}" if level + 1 < CHAIN else None
        lines.append(f"definition chain/t{level} {{")
        lines.append("    relation viewer: auth/user | auth/group#member")
        arms = ["viewer"]
        for arm in range(ARMS):
            lines.append(f"    relation side{arm}: auth/group")
            arms.append(f"side{arm}->member")
        if following is not None:
            lines.append(f"    relation up: {following}")
            arms.append("up->view")
        lines.append(f"    permission view = {' + '.join(arms)}")
        lines.append("}")
    lines += [
        "definition blog/post {",
        "    relation parent: chain/t0",
        "    relation viewer: auth/user",
        "    permission read = viewer + parent->view",
        "}",
    ]
    return "\n".join(lines)


@pytest.fixture
def wide():
    local = install_schema(_wide_schema())
    with sudo(reason="wide policy fixture"):
        post = Post.objects.create(title="reached at the end of the chain")
    tuples = [RelationshipTuple(to_object_ref(post), "parent", SubjectRef.of("chain/t0", "0"))]
    tuples += [
        RelationshipTuple(
            ObjectRef(f"chain/t{level}", "0"), "up", SubjectRef.of(f"chain/t{level + 1}", "0")
        )
        for level in range(CHAIN - 1)
    ]
    tuples.append(RelationshipTuple(ObjectRef(f"chain/t{CHAIN - 1}", "0"), "viewer", ACTOR))
    local.write_relationships(tuples)
    return local, post


def _under_depth_limit(limit: int):
    connection.ensure_connection()
    raw = connection.connection
    previous = raw.getlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH)
    raw.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, limit)
    return raw, previous


def test_a_wide_policy_reads_under_a_fixed_expression_depth(wide):
    local, post = wide
    raw, previous = _under_depth_limit(BOUND)
    try:
        scope = read.scope_q(backend=local, model=Post, action="read", actor=ACTOR, using="default")
        assert list(Post._base_manager.filter(scope).values_list("pk", flat=True)) == [post.pk]
        assert local.check_access(
            subject=ACTOR, action="read", resource=to_object_ref(post)
        ).allowed
    finally:
        raw.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, previous)


def test_nested_id_sets_alone_exceed_sqlites_default_depth(wide, monkeypatch):
    # The same statement with each id set nested in place, as 0.31.0 rendered it.
    local, _post = wide
    monkeypatch.setattr(
        "rebac.compile.predicate._Compiled._statement_local",
        lambda self, sql, params, compiler, connection: (sql, tuple(params)),
    )
    read.reset()
    scope = read.scope_q(backend=local, model=Post, action="read", actor=ACTOR, using="default")
    with pytest.raises(Exception, match="Expression tree is too large"):
        list(Post._base_manager.filter(scope).values_list("pk", flat=True))
