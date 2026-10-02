"""Regressions found reviewing the proposal-0015 prototype."""

from __future__ import annotations

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo
from rebac.compile import read as compiled
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pg_delta]

ALICE = SubjectRef.of("auth/user", "alice")

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    relation editor: auth/user
    permission read = viewer
    permission edit = editor
}
definition blog/post {
    relation col: blog/folder // rebac:field=collections
    permission both = col->read & col->edit
    permission open = authenticated - both
}
"""


def test_intersection_over_one_multivalued_path_agrees_with_the_index():
    """A post in two collections: the actor reads one and edits the other.

    ``both`` holds through two different collections, so ``open`` must not.
    The index and the compiled point check agree; the compiled scope does not.
    """
    local = install_schema(SCHEMA)
    with sudo(reason="review pin fixtures"):
        readable = Folder.objects.create(name="readable")
        editable = Folder.objects.create(name="editable")
        post = Post.objects.create(title="in both")
        post.collections.add(readable, editable)
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("blog/folder", str(readable.pk)), "viewer", ALICE),
            RelationshipTuple(ObjectRef("blog/folder", str(editable.pk)), "editor", ALICE),
        ]
    )
    resource = ObjectRef("blog/post", str(post.pk))

    def answers(action):
        scope = compiled.scope_q(
            backend=local, model=Post, action=action, actor=ALICE, using="default"
        )
        return (
            local.check_access(subject=ALICE, action=action, resource=resource).allowed,
            compiled.check(
                backend=local,
                resource=resource,
                action=action,
                actor=ALICE,
                context=None,
                using="default",
            ).allowed,
            Post._base_manager.filter(scope).exists(),
        )

    assert answers("both") == (True, True, True)
    assert answers("open") == (False, False, False)
