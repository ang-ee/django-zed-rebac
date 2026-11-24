"""Structural delivery at the depth limit (proposal 0020).

A recursion is read to ``REBAC_DEPTH_LIMIT`` hops, counted from where its component was
entered. A parent is admitted with its permission's component entered at the parent. A
child whose own permission is in that component reads the parent's across the delivered
edge, one hop into the component: with one hop less of the limit. Such an edge is refused;
an arrow from outside the recursion is read with the whole limit and is delivered.
"""

from __future__ import annotations

import random

import pytest
from django.db import connection
from django.db.models import Prefetch
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope
from rebac.testing import install_schema
from tests.mechanism_policies import seeds
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")

TREE = """
definition auth/user {}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    permission read = parent->read + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation owner: auth/user
    permission read = folder->read + owner
}
"""

# The folders' recursion runs through stored rows; the posts' arrow into it is a foreign key.
STORED_TREE = """
definition auth/user {}
definition blog/folder {
    relation up: blog/folder
    relation viewer: auth/user
    permission read = up->read + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation owner: auth/user
    permission read = folder->read + owner
}
"""

# The two permissions are one recursion, through the delivered edge ``post.folder``.
MUTUAL = """
definition auth/user {}
definition blog/folder {
    relation pinned: blog/post
    relation viewer: auth/user
    permission read = pinned->read + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation owner: auth/user
    permission read = folder->read + owner
}
"""


# The post's ``read`` is outside the recursion, but the permission it opens on the way to the
# arm, ``base``, is in it with the folder's ``read``.
OPENED = """
definition auth/user {}
definition blog/folder {
    relation pinned: blog/post
    relation viewer: auth/user
    permission read = pinned->base + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation owner: auth/user
    permission base = folder->read
    permission read = base + owner
}
"""


@pytest.fixture(params=["denormalized", "registry"])
def frontier(request, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    settings.REBAC_DEPTH_LIMIT = 2


def grant(type_: str, id_: object, relation: str, subject: SubjectRef = ALICE) -> RelationshipTuple:
    return RelationshipTuple(ObjectRef(type_, str(id_)), relation, subject)


def readable(local, type_: str) -> set[int]:
    return {int(pk) for pk in local.accessible(subject=ALICE, action="read", resource_type=type_)}


def both_ways(settings, read_rows):
    """``read_rows`` with structural prefetch off and on, and the statements of the second."""
    settings.REBAC_STRUCTURAL_PREFETCH = False
    scoped = read_rows()
    settings.REBAC_STRUCTURAL_PREFETCH = True
    with CaptureQueriesContext(connection) as captured:
        delivered = read_rows()
    return scoped, delivered, [query["sql"] for query in captured.captured_queries]


def test_a_tree_read_under_its_own_recursion_keeps_the_childrens_statement(frontier, settings):
    local = install_schema(TREE)
    Folder._base_manager.bulk_create(
        [Folder(id=n, name=f"f{n}", parent_id=n - 1 if n > 1 else None) for n in (1, 2, 3, 4, 5)]
    )
    local.write_relationships([grant("blog/folder", 1, "viewer")])
    # Folder 3 is two hops below the viewer's: admitted. Folder 4, three hops: not readable.
    assert readable(local, "blog/folder") == {1, 2, 3}

    def children():
        rows = Folder.objects.with_actor(ALICE).rebac_prefetch_related("children")
        return {f.pk: sorted(c.pk for c in f.children.all()) for f in rows}

    scoped, delivered, statements = both_ways(settings, children)
    assert scoped == {1: [2], 2: [3], 3: []}
    assert delivered == scoped
    # The children keep their permission statement (the parents' is the first).
    assert sum("rebac_" in sql for sql in statements if 'FROM "testapp_folder"' in sql) == 2


@pytest.mark.parametrize("schema", [TREE, STORED_TREE], ids=["a foreign key", "stored rows"])
def test_an_arrow_from_outside_the_recursion_is_delivered_at_the_frontier(
    frontier, settings, schema
):
    local = install_schema(schema)
    Folder._base_manager.bulk_create(
        [Folder(id=n, name=f"f{n}", parent_id=n - 1 if n > 1 else None) for n in (1, 2, 3, 4)]
    )
    Post._base_manager.bulk_create([Post(id=n, title=f"p{n}", folder_id=n) for n in (1, 2, 3, 4)])
    local.write_relationships(
        [
            grant("blog/folder", 1, "viewer"),
            *(
                grant("blog/folder", n, "up", SubjectRef.of("blog/folder", str(n - 1)))
                for n in (2, 3, 4)
                if schema is STORED_TREE
            ),
        ]
    )
    # The post of folder 3, at the frontier, enters the recursion afresh at its folder.
    assert readable(local, "blog/folder") == {1, 2, 3}
    assert readable(local, "blog/post") == {1, 2, 3}

    def posts():
        rows = Folder.objects.with_actor(ALICE).rebac_prefetch_related("posts")
        return {f.pk: sorted(p.pk for p in f.posts.all()) for f in rows}

    scoped, delivered, statements = both_ways(settings, posts)
    assert scoped == {1: [1], 2: [2], 3: [3]}
    assert delivered == scoped
    child = next(sql for sql in statements if 'FROM "testapp_post"' in sql)
    assert "rebac_" not in child


@pytest.mark.parametrize("schema", [MUTUAL, OPENED], ids=["the child's action", "a name it opens"])
def test_a_mutual_recursion_through_the_delivered_edge_keeps_the_childrens_statement(
    frontier, settings, schema
):
    local = install_schema(schema)
    # The viewer's folder 1 <- post 2 in it <- folder 2 pins post 2 <- post 3 in folder 2 <- ...
    Folder._base_manager.bulk_create([Folder(id=n, name=f"f{n}") for n in (1, 2, 3, 4)])
    Post._base_manager.bulk_create(
        [Post(id=n, title=f"p{n}", folder_id=n - 1 if n > 1 else None) for n in (1, 2, 3, 4, 5)]
    )
    local.write_relationships(
        [
            grant("blog/folder", 1, "viewer"),
            *(
                grant("blog/folder", n, "pinned", SubjectRef.of("blog/post", str(n)))
                for n in (1, 2, 3, 4)
            ),
        ]
    )
    # Folder 2 is admitted through post 2, two hops from the viewer's folder; post 3 in it,
    # three hops, is not readable.
    assert readable(local, "blog/folder") == {1, 2}
    assert readable(local, "blog/post") == {2}

    def contents():
        rows = Folder.objects.with_actor(ALICE).rebac_prefetch_related("posts")
        return {f.pk: sorted(p.pk for p in f.posts.all()) for f in rows}

    scoped, delivered, statements = both_ways(settings, contents)
    assert scoped == {1: [2], 2: []}
    assert delivered == scoped
    assert "rebac_" in next(sql for sql in statements if 'FROM "testapp_post"' in sql)


# ---------- Randomized: delivery reads what the scoped read does, at every depth limit ----------

# Each shape: its definitions beside ``auth/user``, the folder actions parents are admitted
# under, and the post actions their posts are read under.
SHAPES = {
    # An arrow from another type into a recursion over a foreign key.
    "field_tree": (
        """
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    relation editor: auth/user
    permission read = parent->read + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation owner: auth/user
    permission read = folder->read + owner
}
""",
        ("read",),
        ("read",),
    ),
    # The same over a stored parent relation.
    "stored_tree": (
        """
definition blog/folder {
    relation up: blog/folder
    relation viewer: auth/user
    permission read = up->read + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
""",
        ("read",),
        ("read",),
    ),
    # A recursion over a filtered foreign key.
    "filtered_tree": (
        """
definition blog/folder {
    relation parent: blog/folder // rebac:field={"path":"parent","filters":{"parent__is_active":true}}
    relation viewer: auth/user
    permission read = parent->read + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
""",
        ("read",),
        ("read",),
    ),
    # A recursion over a path of two foreign keys (the grandparent): one hop to the compiler.
    "grandparent_tree": (
        """
definition blog/folder {
    relation grand: blog/folder // rebac:field=parent__parent
    relation viewer: auth/user
    permission read = grand->read + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
""",
        ("read",),
        ("read",),
    ),
    # Two recursive components in sequence: the folder tree, then role inclusion.
    "two_components": (
        """
definition test/role {
    relation member: auth/user
    relation includes: test/role
    permission eff = member + includes->eff
}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation role: test/role
    relation viewer: auth/user
    permission read = (parent->read + role->eff) + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
""",
        ("read",),
        ("read",),
    ),
    # A recursion across two types, entered from a third.
    "two_type_component": (
        """
definition test/drive {
    relation home: blog/folder
    relation keeper: auth/user
    permission read = home->read + keeper
}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation drive: test/drive
    relation viewer: auth/user
    permission read = drive->read + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
""",
        ("read",),
        ("read",),
    ),
    # The child's own read is recursive too (over a stored relation), the arm beside it.
    "recursive_child": (
        """
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    permission read = parent->read + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation up: blog/post
    relation owner: auth/user
    permission read = (up->read + folder->read) + owner
}
""",
        ("read",),
        ("read",),
    ),
    # A mutual recursion through the delivered edge, the arm behind a named permission.
    "mutual_opened": (
        """
definition blog/folder {
    relation pinned: blog/post
    relation viewer: auth/user
    permission read = pinned->read + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation owner: auth/user
    permission base = folder->read
    permission read = base + owner
}
""",
        ("read",),
        ("read",),
    ),
    # The recursion holds a permission the child's union opens, not the child's action.
    "opened_only": (
        """
definition blog/folder {
    relation pinned: blog/post
    relation viewer: auth/user
    permission read = pinned->base + viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation owner: auth/user
    permission base = folder->read
    permission read = base + owner
}
""",
        ("read",),
        ("read",),
    ),
    # Parents admitted under a recursive permission, or under one that reads it.
    "over_a_recursion": (
        """
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    relation editor: auth/user
    permission tree = parent->tree + viewer
    permission read = (parent->tree + viewer) + editor
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
""",
        ("tree", "read"),
        ("read",),
    ),
    # Parents admitted under a permission of a mutual recursion, or one beside it.
    "over_a_mutual_recursion": (
        """
definition blog/folder {
    relation pinned: blog/post
    relation viewer: auth/user
    relation editor: auth/user
    permission read = pinned->read + viewer
    permission view = (pinned->read + viewer) + editor
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation owner: auth/user
    permission read = folder->read + owner
    permission look = folder->view
}
""",
        ("read", "view"),
        ("read", "look"),
    ),
    # One permission enters the recursion twice, on two branches, one a hop further.
    "entered_twice": (
        """
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    permission read = parent->read + viewer
    permission view = read + parent->read
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->view
}
""",
        ("view", "read"),
        ("read",),
    ),
}


def random_rows(rng: random.Random, schema: str) -> list[RelationshipTuple]:
    """Eight folders in random chains, ten posts among them, and random grants of ``schema``'s
    relations to two actors."""
    Folder._base_manager.bulk_create(
        [
            Folder(
                id=n,
                name=f"f{n}",
                parent_id=rng.choice([None, *range(max(1, n - 2), n)]) if n > 1 else None,
                is_active=rng.random() < 0.8,
            )
            for n in range(1, 9)
        ]
    )
    Post._base_manager.bulk_create(
        [Post(id=n, title=f"p{n}", folder_id=rng.randint(1, 8)) for n in range(1, 11)]
    )
    tuples: list[RelationshipTuple] = []

    def maybe(chance: float, type_: str, id_: object, relation: str, subject: SubjectRef) -> None:
        if rng.random() < chance:
            tuples.append(grant(type_, id_, relation, subject))

    for actor in (ALICE, BOB):
        for n in range(1, 9):
            maybe(0.15, "blog/folder", n, "viewer", actor)
            if "relation editor" in schema:
                maybe(0.1, "blog/folder", n, "editor", actor)
        if "relation owner" in schema:
            for n in range(1, 11):
                maybe(0.1, "blog/post", n, "owner", actor)
    if "relation up: blog/folder" in schema:
        for n in range(2, 9):
            up = SubjectRef.of("blog/folder", str(rng.randint(max(1, n - 2), n - 1)))
            maybe(0.8, "blog/folder", n, "up", up)
    if "relation up: blog/post" in schema:
        for n in range(2, 11):
            maybe(0.6, "blog/post", n, "up", SubjectRef.of("blog/post", str(rng.randint(1, n - 1))))
    if "relation pinned" in schema:
        for n in range(1, 9):
            pinned = SubjectRef.of("blog/post", str(rng.randint(1, 10)))
            maybe(0.6, "blog/folder", n, "pinned", pinned)
    if "test/drive" in schema:
        for n in range(1, 4):
            home = SubjectRef.of("blog/folder", str(rng.randint(1, 8)))
            tuples.append(grant("test/drive", f"d{n}", "home", home))
            maybe(0.4, "test/drive", f"d{n}", "keeper", rng.choice([ALICE, BOB]))
        for n in range(1, 9):
            drive = SubjectRef.of("test/drive", f"d{rng.randint(1, 3)}")
            maybe(0.4, "blog/folder", n, "drive", drive)
    if "test/role" in schema:
        for n in range(1, 6):
            included = SubjectRef.of("test/role", f"r{rng.randint(1, 5)}")
            maybe(0.7, "test/role", f"r{n}", "includes", included)
        for actor in (ALICE, BOB):
            maybe(0.5, "test/role", f"r{rng.randint(1, 5)}", "member", actor)
        for n in range(1, 9):
            role = SubjectRef.of("test/role", f"r{rng.randint(1, 5)}")
            maybe(0.3, "blog/folder", n, "role", role)
    return tuples


def posts_of(actor: SubjectRef, action: str, child_action: str):
    rows = (
        Folder.objects.with_actor(actor)
        .with_action(action)
        .rebac_prefetch_related(
            Prefetch("posts", queryset=Post.objects.with_actor(actor).with_action(child_action))
        )
    )
    return {f.pk: sorted(p.pk for p in f.posts.all()) for f in rows}


def children_of(actor: SubjectRef, action: str):
    rows = Folder.objects.with_actor(actor).with_action(action).rebac_prefetch_related("children")
    return {f.pk: sorted(c.pk for c in f.children.all()) for f in rows}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("seed", seeds(5, fast=1))
def test_delivery_reads_what_the_scoped_read_does_at_the_frontier(
    seed, shape, depth, storage, settings
):
    """Random chains of folders, deeper than the limit, under recursions entered from outside,
    through the delivered edge, through a name the child's union opens, twice, beside another
    component and across types: with the setting on, every prefetch of posts and of subfolders
    returns what it returns with the setting off, inside an evaluator scope and outside one."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    settings.REBAC_DEPTH_LIMIT = depth
    schema, actions, child_actions = SHAPES[shape]
    rng = random.Random(seed * 1000 + depth)
    local = install_schema("definition auth/user {}\n" + schema)
    tuples = random_rows(rng, schema)
    if tuples:
        local.write_relationships(tuples)
    for actor in (ALICE, BOB):
        for action in actions:
            settings.REBAC_STRUCTURAL_PREFETCH = False
            expected_children = children_of(actor, action)
            expected = {child: posts_of(actor, action, child) for child in child_actions}
            settings.REBAC_STRUCTURAL_PREFETCH = True
            assert children_of(actor, action) == expected_children, (actor, action)
            for child, posts in expected.items():
                assert posts_of(actor, action, child) == posts, (actor, action, child)
                with evaluator_scope():
                    assert posts_of(actor, action, child) == posts, (actor, action, child)
