"""An exclusion inside an exclusion, on the ids that stored tuples name (accessible())."""

from __future__ import annotations

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db

ALICE = SubjectRef.of("auth/user", "alice")

SCHEMA = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    relation reader: auth/user
    relation hidden: auth/user
    relation keeper: auth/user
    permission conceal = hidden - keeper
    permission read = (owner + reader) - conceal
}
"""


@pytest.fixture
def posts():
    local = install_schema(SCHEMA)
    with sudo(reason="test.fixture"):
        shown, concealed, kept = (Post.objects.create(title=name) for name in ("a", "b", "c"))
    rows = [
        (shown, "reader"),
        (concealed, "reader"),
        (concealed, "hidden"),
        (kept, "reader"),
        (kept, "hidden"),
        (kept, "keeper"),
    ]
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("blog/post", str(post.pk)), name, ALICE)
            for post, name in rows
        ]
    )
    # A resource that only tuples name: no row of the model.
    local.write_relationships(
        [RelationshipTuple(ObjectRef("blog/post", "tuple-only"), "reader", ALICE)]
    )
    return local, shown, concealed, kept


def test_accessible_reads_a_nested_exclusion(posts):
    local, shown, concealed, kept = posts
    expected = {str(shown.pk), str(kept.pk), "tuple-only"}
    assert (
        set(local.accessible(subject=ALICE, action="read", resource_type="blog/post")) == expected
    )
    # The point checks and the scope agree.
    for post, allowed in ((shown, True), (concealed, False), (kept, True)):
        resource = ObjectRef("blog/post", str(post.pk))
        assert (
            local.check_access(subject=ALICE, action="read", resource=resource).allowed is allowed
        )
    assert set(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == {shown.pk, kept.pk}


ARROW_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation hidden: auth/user
    relation keeper: auth/user
    permission withheld = hidden - keeper
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation reader: auth/user
    permission read = reader - folder->withheld
}
"""


def test_an_exclusion_reached_through_an_arrow(db):
    local = install_schema(ARROW_SCHEMA)
    with sudo(reason="test.fixture"):
        open_, withheld, kept = (
            Folder.objects.create(name=n) for n in ("open", "withheld", "kept")
        )
        posts = {
            f.name: Post.objects.create(title=f.name, folder=f) for f in (open_, withheld, kept)
        }
    tuples = [
        RelationshipTuple(ObjectRef("blog/post", str(p.pk)), "reader", ALICE)
        for p in posts.values()
    ]
    tuples += [
        RelationshipTuple(ObjectRef("blog/folder", str(withheld.pk)), "hidden", ALICE),
        RelationshipTuple(ObjectRef("blog/folder", str(kept.pk)), "hidden", ALICE),
        RelationshipTuple(ObjectRef("blog/folder", str(kept.pk)), "keeper", ALICE),
    ]
    local.write_relationships(tuples)
    expected = {str(posts["open"].pk), str(posts["kept"].pk)}
    assert (
        set(local.accessible(subject=ALICE, action="read", resource_type="blog/post")) == expected
    )


DEEP_SCHEMA = """
definition auth/user {}
definition blog/post {
    relation a: auth/user
    relation b: auth/user
    relation c: auth/user
    relation d: auth/user
    permission read = a - (b - (c - d))
}
"""


def test_three_exclusions_deep(db):
    local = install_schema(DEEP_SCHEMA)
    with sudo(reason="test.fixture"):
        rows = {name: Post.objects.create(title=name) for name in ("a", "ab", "abc", "abcd")}
    tuples = [
        RelationshipTuple(ObjectRef("blog/post", str(post.pk)), relation, ALICE)
        for name, post in rows.items()
        for relation in name
    ]
    local.write_relationships(tuples)
    # a holds; b takes it away; c gives it back; d takes c away.
    expected = {str(rows["a"].pk), str(rows["abc"].pk)}
    assert (
        set(local.accessible(subject=ALICE, action="read", resource_type="blog/post")) == expected
    )
    assert {
        str(pk) for pk in Post.objects.with_actor(ALICE).values_list("pk", flat=True)
    } == expected
