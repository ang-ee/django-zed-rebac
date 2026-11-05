import pytest
from django.db import connection
from django.db.models import Exists, OuterRef
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, decision_scope, sudo, to_object_ref
from rebac.testing import install_schema
from tests.test_compile_rows import assert_rebac_ctes_defined
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


@pytest.mark.parametrize("shape", ["standalone", "pk_in", "exists"])
def test_parent_every_row_gate(shape):
    local = install_schema("""
        definition auth/user {}
        definition org/role {
            relation member: auth/user
        }
        definition blog/folder {
            relation viewer: auth/user
            permission read = viewer
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            relation admin: org/role // rebac:const=admin
            permission read = folder->read + admin->member
        }
    """)
    admin = SubjectRef.of("auth/user", "admin")
    with sudo(reason="every row gate fixture"):
        folders = Folder.objects.bulk_create([Folder(name=f"folder-{i}") for i in range(6)])
        posts = Post.objects.bulk_create(
            [Post(title=f"post-{i}", folder=folder) for i, folder in enumerate(folders)]
        )
    local.write_relationships(
        [RelationshipTuple(ObjectRef("org/role", "admin"), "member", admin)]
        + [RelationshipTuple(to_object_ref(folder), "viewer", admin) for folder in folders[:2]]
    )

    with CaptureQueriesContext(connection) as queries:
        if shape == "standalone":
            result = sorted(Post.objects.with_actor(admin).values_list("pk", flat=True))
            expected = sorted(post.pk for post in posts)
        elif shape == "pk_in":
            with decision_scope():
                result = sorted(
                    Post.objects.with_actor(admin)
                    .filter(pk__in={posts[0].pk, posts[3].pk})
                    .values_list("pk", flat=True)
                )
            expected = sorted([posts[0].pk, posts[3].pk])
        else:
            scoped = Post.objects.with_actor(admin).scoped()
            result = sorted(
                Folder._base_manager.filter(
                    Exists(scoped.filter(folder_id=OuterRef("pk")))
                ).values_list("pk", flat=True)
            )
            expected = sorted(folder.pk for folder in folders)
    assert result == expected
    assert_rebac_ctes_defined(queries)
    if connection.vendor == "postgresql":
        assert any('FROM "rebac_decided_' in query["sql"] for query in queries)
    if shape == "exists":
        assert len(queries) == 3
