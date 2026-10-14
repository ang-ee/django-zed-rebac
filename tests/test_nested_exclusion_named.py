"""An exclusion inside an exclusion, at identities that are not rows of the type's model."""

from __future__ import annotations

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo
from rebac.testing import install_schema
from tests.testapp.models import Post

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
    relation x: auth/user
    permission withheld = hidden - (keeper - x)
}
definition blog/post {
    relation folder: blog/folder
    relation reader: auth/user
    permission read = reader - folder->withheld
}
"""


def test_a_nested_exclusion_behind_a_stored_arrow(db):
    """The folder is a stored relation: its exclusions are evaluated at the
    folder ids that tuples name, in point checks and scopes as in accessible()."""
    local = install_schema(ARROW_SCHEMA)
    with sudo(reason="test.fixture"):
        posts = {name: Post.objects.create(title=name) for name in ("open", "withheld", "kept")}
    folder = {name: ObjectRef("blog/folder", name) for name in posts}
    tuples = [
        RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "reader", ALICE)
        for post in posts.values()
    ]
    tuples += [
        RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "folder", _folder(folder[name]))
        for name, post in posts.items()
    ]
    tuples += [
        RelationshipTuple(folder["withheld"], "hidden", ALICE),
        RelationshipTuple(folder["kept"], "hidden", ALICE),
        RelationshipTuple(folder["kept"], "keeper", ALICE),
    ]
    local.write_relationships(tuples)
    expected = {str(posts["open"].pk), str(posts["kept"].pk)}
    assert (
        set(local.accessible(subject=ALICE, action="read", resource_type="blog/post")) == expected
    )
    for post in posts.values():
        resource = ObjectRef("blog/post", str(post.pk))
        allowed = str(post.pk) in expected
        assert (
            local.check_access(subject=ALICE, action="read", resource=resource).allowed is allowed
        )
    assert {
        str(pk) for pk in Post.objects.with_actor(ALICE).values_list("pk", flat=True)
    } == expected


def _folder(ref):
    return SubjectRef.of(ref.resource_type, ref.resource_id)


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
