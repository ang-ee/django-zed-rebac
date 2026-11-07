"""A union that names a union compiles as one union.

At a row the compiler writes a union as ``id IN (SELECT … UNION …)``.  A named
arm compiled as its own union nests one more such level on every path through
it, and SQLite adds the height of every enclosing subquery to an expression's
depth: ``read = granted + chatter`` reached through an arrow was one level
deeper than ``read = granted``.  A union now takes the parts of each union it
names, from the same memoized nodes and in the same order, so the statement is
no deeper than the flat union and compiling it reads the database exactly as
compiling the nested form did.
"""

from __future__ import annotations

import sqlite3

import pytest
from django.db import connection
from django.db.models import IntegerField, Value
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.compile import read
from rebac.compile.predicate import Compiler
from rebac.schema.ast import PermBinOp
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db

ADMIN = SubjectRef.of("auth/user", "admin")
MEMBER = SubjectRef.of("auth/user", "member")
OWNER = SubjectRef.of("auth/user", "owner")
OUTSIDER = SubjectRef.of("auth/user", "outsider")
ACTORS = (ADMIN, MEMBER, OWNER, OUTSIDER)

_HEAD = """
definition auth/user {}
definition auth/group { relation member: auth/user | auth/group#member }
definition org/role { relation member: auth/user }
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation viewer: auth/user
    permission read = viewer + folder->read
}
"""
# Two named unions that share ``editor``, as a thread's ``read_granted`` and
# ``chatter_read``; the flat form lists the same arms in the same order.
_FOLDER = """
definition blog/folder {
    relation owner: auth/user
    relation editor: auth/user | auth/group#member
    relation commenter: auth/user | auth/group#member
    relation admin: org/role // rebac:const=admin
    relation staff: org/role // rebac:const=staff
    %s
}
"""
COMPOSITE = _HEAD + _FOLDER % (
    "permission granted = admin->member + owner + editor\n"
    "    permission chatter = staff->member + commenter + editor\n"
    "    permission read = granted + chatter"
)
FLAT = _HEAD + _FOLDER % (
    "permission read = admin->member + owner + editor + staff->member + commenter + editor"
)
# The same names over a recursion through the parent folder: the per-key memo
# and the depth bound's accounting run through the spliced names.
RECURSIVE = _HEAD + _FOLDER.replace(
    "    %s", "    relation parent: blog/folder // rebac:field=parent\n    %s"
) % (
    "permission granted = admin->member + owner + editor + parent->read\n"
    "    permission chatter = staff->member + commenter + editor\n"
    "    permission read = granted + chatter"
)


def _unspliced(self, definition, expr, at, bound, visits, *, depth_possible):
    """The compile before a union took a named union's parts: one union per name."""
    if isinstance(expr, PermBinOp) and expr.op == "+":
        arms = self._union_arms(expr)
        if len(arms) > 1:
            return self._arms(definition, arms, at, bound, visits, depth_possible)
    return [self._expression(definition, expr, at, bound, visits, depth_possible=depth_possible)]


def _install(schema: str):
    local = install_schema(schema)
    with sudo(reason="named union splice fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        posts = [
            Post.objects.create(title="in root", folder=root),
            Post.objects.create(title="in child", folder=child),
            Post.objects.create(title="loose"),
        ]
    group = ObjectRef("auth/group", "readers")
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("org/role", "admin"), "member", ADMIN),
            RelationshipTuple(group, "member", MEMBER),
            RelationshipTuple(
                to_object_ref(root), "commenter", SubjectRef.of("auth/group", "readers", "member")
            ),
            RelationshipTuple(to_object_ref(child), "owner", OWNER),
            RelationshipTuple(to_object_ref(posts[2]), "viewer", OUTSIDER),
        ]
    )
    return local, posts


def _scope(local, actor: SubjectRef, probe: int = 0):
    scope = read.scope_q(backend=local, model=Post, action="read", actor=actor, using="default")
    rows = Post._base_manager.filter(scope)
    if probe:
        # A new selected column each time: SQLite checks the depth when it
        # prepares a statement, and keeps a prepared statement by its text.
        rows = rows.annotate(**{f"probe_{probe}": Value(probe, output_field=IntegerField())})
    return rows


def _depth(local, actor: SubjectRef) -> int:
    """The smallest SQLITE_LIMIT_EXPR_DEPTH under which the actor's scope reads."""
    connection.ensure_connection()
    raw = connection.connection
    previous = raw.getlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH)
    probe = iter(range(1, 10**6))
    low, high = 1, 1000
    try:
        while low < high:
            middle = (low + high) // 2
            raw.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, middle)
            try:
                list(_scope(local, actor, next(probe)))
            except Exception as error:  # only the depth refusal is expected
                assert "too large" in str(error), error
                low = middle + 1
            else:
                high = middle
    finally:
        raw.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, previous)
    return low


@pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite's expression depth")
def test_an_arrow_to_two_named_unions_is_no_deeper_than_the_flat_union(monkeypatch):
    flat = _depth(_install(FLAT)[0], MEMBER)
    local, _posts = _install(COMPOSITE)
    read.reset()
    composite = _depth(local, MEMBER)
    assert composite <= flat
    # Compiled one union per name, the same policy nests one level deeper.
    monkeypatch.setattr(Compiler, "_disjuncts", _unspliced)
    read.reset()
    assert _depth(local, MEMBER) > flat


def _reads(local, posts) -> tuple[list[int], list[object]]:
    """Every actor's scope and point checks, compiled afresh, then kept: statements and answers."""
    read.reset()
    counts: list[int] = []
    answers: list[object] = []
    for _pass in ("compiled", "kept"):
        with CaptureQueriesContext(connection) as queries:
            for actor in ACTORS:
                answers.append(sorted(_scope(local, actor).values_list("pk", flat=True)))
                answers.extend(
                    local.check_access(
                        subject=actor, action="read", resource=to_object_ref(post)
                    ).allowed
                    for post in posts
                )
        counts.append(len(queries))
    return counts, answers


@pytest.mark.parametrize("schema", [COMPOSITE, RECURSIVE], ids=["composite", "recursive"])
def test_the_splice_reads_and_answers_exactly_as_one_union_per_name(monkeypatch, settings, schema):
    # A recursion through a name unrolls to the depth limit as nested SQL.
    settings.REBAC_DEPTH_LIMIT = 3
    local, posts = _install(schema)
    spliced = _reads(local, posts)
    monkeypatch.setattr(Compiler, "_disjuncts", _unspliced)
    assert _reads(local, posts) == spliced
    # Both passes answer alike; the first pass's answers, by actor.
    assert spliced[1][:16] == spliced[1][16:]
    admin, member, owner, outsider = (spliced[1][i * 4 : i * 4 + 4] for i in range(4))
    root, child, loose = (post.pk for post in posts)
    assert admin == [[root, child], True, True, False]
    assert member == [
        [root, *([child] if schema is RECURSIVE else [])],
        True,
        schema is RECURSIVE,
        False,
    ]
    assert owner == [[child], False, True, False]
    assert outsider == [[loose], False, False, True]
