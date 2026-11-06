"""Statement-local CTEs preserve empty subject sets in negated arms."""

import pytest

from rebac import RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db


def test_empty_subject_set_in_negated_arm_keeps_outer_row():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            permission ban = nil
        }
        definition blog/post {
            relation viewer: auth/user
            relation folders: blog/folder // rebac:field=collections
            permission read = viewer - folders->ban
        }
    """)
    actor = SubjectRef.of("auth/user", "empty-cte")
    with sudo(reason="empty CTE fixture"):
        folder = Folder.objects.create(name="folder")
        post = Post.objects.create(title="visible")
        post.collections.add(folder)
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", actor)])
    assert list(Post.objects.with_actor(actor).values_list("pk", flat=True)) == [post.pk]
    assert local.check_access(subject=actor, action="read", resource=to_object_ref(post)).allowed
