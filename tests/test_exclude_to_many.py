"""``exclude()`` across a to-many relation on an actor-scoped queryset.

Django answers such an exclude with ``NOT EXISTS`` over an inner query it
builds itself (``Query.split_exclude``), from the class of the outer query.
That inner query is part of the outer statement, not an embedded queryset.
"""

import pytest
from django.db.models import Exists, OuterRef

from rebac import MissingActorError, actor_context, sudo, to_object_ref
from rebac.testing import install_schema
from rebac.types import RelationshipTuple, SubjectRef
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.pg_delta  # the statement Django compiles must also run on PostgreSQL

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
"""


@pytest.fixture
def rows(db):
    local = install_schema(SCHEMA)
    with sudo(reason="exclude fixtures"):
        mixed = Folder.objects.create(name="mixed")
        clean = Folder.objects.create(name="clean")
        hidden = Folder.objects.create(name="hidden")
        archive = Folder.objects.create(name="archive")
        local.write_relationships(
            [RelationshipTuple(to_object_ref(f), "viewer", ALICE) for f in (mixed, clean)]
        )
        posts = {
            title: Post.objects.create(title=title, folder=folder)
            for title, folder in (
                ("keep", mixed),
                ("drop", mixed),
                ("fine", clean),
                ("unseen", hidden),
            )
        }
        posts["keep"].collections.add(archive)
    return {"mixed": mixed, "clean": clean, "hidden": hidden, **posts}


def _names(queryset, attr):
    return sorted(getattr(row, attr) for row in queryset)


def test_exclude_across_a_reverse_foreign_key(rows):
    excluded = Folder.objects.with_actor(ALICE).exclude(posts__title="drop")
    explicit = Folder.objects.with_actor(ALICE).filter(
        ~Exists(Post._base_manager.filter(folder=OuterRef("pk"), title="drop"))
    )

    assert _names(excluded, "name") == _names(explicit, "name") == ["clean"]


def test_exclude_across_a_foreign_key_then_a_reverse_foreign_key(rows):
    excluded = Post.objects.with_actor(ALICE).exclude(folder__posts__title="drop")

    assert _names(excluded, "title") == ["fine"]


def test_exclude_across_a_many_to_many(rows):
    excluded = Post.objects.with_actor(ALICE).exclude(collections__name="archive")

    assert _names(excluded, "title") == ["drop", "fine"]


def test_exclude_under_the_ambient_actor_and_without_one(rows):
    with actor_context(ALICE):
        assert _names(Folder.objects.exclude(posts__title="drop"), "name") == ["clean"]
    with actor_context(BOB):
        assert _names(Folder.objects.exclude(posts__title="drop"), "name") == []
    with pytest.raises(MissingActorError):
        list(Folder.objects.exclude(posts__title="drop"))


def test_a_queryset_embedded_in_the_exclude_keeps_its_own_scope(rows):
    dropped = Post.objects.filter(title="drop")

    as_alice = Folder.objects.with_actor(ALICE).exclude(posts__in=dropped.with_actor(ALICE))
    as_bob = Folder.objects.with_actor(ALICE).exclude(posts__in=dropped.with_actor(BOB))

    assert _names(as_alice, "name") == ["clean"]
    # Bob reads no post, so his subquery is empty and excludes nothing.
    assert _names(as_bob, "name") == ["clean", "mixed"]
    with pytest.raises(MissingActorError):
        list(Folder.objects.with_actor(ALICE).exclude(posts__in=dropped))
