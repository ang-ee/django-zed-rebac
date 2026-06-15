"""Field-backed relations read structural Django FKs instead of tuple rows."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import (
    LocalBackend,
    ObjectRef,
    RelationshipTuple,
    SchemaError,
    SubjectRef,
    sudo,
)
from rebac.models import Relationship, SchemaDefinition, SchemaPermission, SchemaRelation
from rebac.schema import parse_zed
from rebac.types import RelationshipFilter

from .testapp.models import AuthoredPost, Folder, Post

SCHEMA_TEXT = """
definition auth/user {}

definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}

definition blog/post {
    relation owner: auth/user
    relation folder: blog/folder // rebac:field=folder

    permission read = folder->read + owner
}
"""


@pytest.fixture
def backend(db):
    b = LocalBackend()
    b.set_schema(parse_zed(SCHEMA_TEXT))
    return b


def _user(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/user", id_)


def _folder_subject(folder: Folder) -> SubjectRef:
    return SubjectRef.of("blog/folder", str(folder.pk))


def _post_ref(post: Post) -> ObjectRef:
    return ObjectRef("blog/post", str(post.pk))


@pytest.fixture
def rows(db):
    with sudo(reason="field-backed-relations.fixture"):
        visible_folder = Folder.objects.create(name="visible")
        hidden_folder = Folder.objects.create(name="hidden")
        visible_post = Post.objects.create(title="visible", folder=visible_folder)
        hidden_post = Post.objects.create(title="hidden", folder=hidden_folder)
        owned_post = Post.objects.create(title="owned")
        null_post = Post.objects.create(title="null")
    return visible_folder, hidden_folder, visible_post, hidden_post, owned_post, null_post


def test_direct_field_backed_relation_reads_forward_fk_without_tuple(backend, rows):
    visible_folder, _hidden_folder, visible_post, _hidden_post, _owned_post, null_post = rows

    with CaptureQueriesContext(connection) as queries:
        assert backend.has_access(
            subject=_folder_subject(visible_folder),
            action="folder",
            resource=_post_ref(visible_post),
        )
        assert not backend.has_access(
            subject=_folder_subject(visible_folder),
            action="folder",
            resource=_post_ref(null_post),
        )

    assert not Relationship.objects.filter(
        resource_type="blog/post",
        relation="folder",
    ).exists()
    relationship_queries = [q["sql"] for q in queries if "rebac_relationship" in q["sql"].lower()]
    assert relationship_queries == []


def test_arrow_walks_field_backed_relation_to_target_permission(backend, rows):
    visible_folder, _hidden_folder, visible_post, hidden_post, _owned_post, null_post = rows
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/folder", str(visible_folder.pk)),
                relation="viewer",
                subject=_user("alice"),
            )
        ]
    )

    assert backend.has_access(
        subject=_user("alice"), action="read", resource=_post_ref(visible_post)
    )
    assert not backend.has_access(
        subject=_user("alice"), action="read", resource=_post_ref(hidden_post)
    )
    assert not backend.has_access(
        subject=_user("alice"), action="read", resource=_post_ref(null_post)
    )


def test_accessible_unions_field_backed_arrow_and_tuple_grants(backend, rows):
    visible_folder, _hidden_folder, visible_post, _hidden_post, owned_post, _null_post = rows
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/folder", str(visible_folder.pk)),
                relation="viewer",
                subject=_user("alice"),
            ),
            RelationshipTuple(
                resource=_post_ref(owned_post),
                relation="owner",
                subject=_user("alice"),
            ),
        ]
    )

    ids = set(backend.accessible(subject=_user("alice"), action="read", resource_type="blog/post"))
    assert ids == {str(visible_post.pk), str(owned_post.pk)}


def test_lookup_subjects_reads_field_backed_relation_from_column(backend, rows):
    visible_folder, _hidden_folder, visible_post, _hidden_post, _owned_post, null_post = rows

    subjects = list(
        backend.lookup_subjects(
            resource=_post_ref(visible_post),
            action="folder",
            subject_type="blog/folder",
        )
    )
    assert subjects == [_folder_subject(visible_folder)]

    assert (
        list(
            backend.lookup_subjects(
                resource=_post_ref(null_post),
                action="folder",
                subject_type="blog/folder",
            )
        )
        == []
    )


def test_tuple_writes_to_field_backed_relation_are_rejected(backend, rows):
    visible_folder, _hidden_folder, visible_post, _hidden_post, _owned_post, _null_post = rows

    with pytest.raises(SchemaError, match=r"field-backed.*set `Post.folder` instead"):
        backend.write_relationships(
            [
                RelationshipTuple(
                    resource=_post_ref(visible_post),
                    relation="folder",
                    subject=_folder_subject(visible_folder),
                )
            ]
        )

    with pytest.raises(SchemaError, match=r"field-backed.*set `Post.folder` instead"):
        backend.delete_relationships(
            RelationshipFilter(
                resource_type="blog/post",
                resource_id=str(visible_post.pk),
                relation="folder",
            )
        )


def test_unresolved_field_backing_does_not_fall_back_to_stale_tuples(db, rows):
    _visible_folder, _hidden_folder, visible_post, _hidden_post, _owned_post, _null_post = rows
    backend = LocalBackend()
    backend.set_schema(
        parse_zed(
            """
            definition auth/user {
                permission read = authenticated
            }

            definition blog/post {
                relation folder: auth/user // rebac:field=folder
                permission read = folder->read
            }
            """
        )
    )
    Relationship.objects.create(
        resource_type="blog/post",
        resource_id=str(visible_post.pk),
        relation="folder",
        subject_type="auth/user",
        subject_id="alice",
    )

    expected = "blog/post#folder: field-backed relation could not be resolved"
    with pytest.raises(SchemaError, match=expected):
        backend.has_access(
            subject=_user("alice"),
            action="folder",
            resource=_post_ref(visible_post),
        )
    with pytest.raises(SchemaError, match=expected):
        backend.has_access(
            subject=_user("alice"),
            action="read",
            resource=_post_ref(visible_post),
        )
    with pytest.raises(SchemaError, match=expected):
        list(backend.accessible(subject=_user("alice"), action="read", resource_type="blog/post"))
    with pytest.raises(SchemaError, match=expected):
        list(
            backend.lookup_subjects(
                resource=_post_ref(visible_post),
                action="folder",
                subject_type="auth/user",
            )
        )


@override_settings(REBAC_USER_ID_ATTR="username")
def test_field_backed_relation_to_auth_user_honors_subject_id_attr(db):
    backend = LocalBackend()
    backend.set_schema(
        parse_zed(
            """
            definition auth/user {}

            definition blog/authoredpost {
                relation author: auth/user // rebac:field=author
                permission read = author
            }
            """
        )
    )
    user = get_user_model().objects.create(username="alice", is_active=True)
    with sudo(reason="field-backed-relations.auth-user"):
        post = AuthoredPost.objects.create(title="authored", author=user)

    alice = SubjectRef.of("auth/user", "alice")
    post_ref = ObjectRef("blog/authoredpost", str(post.pk))

    assert backend.has_access(subject=alice, action="author", resource=post_ref)
    assert set(
        backend.accessible(subject=alice, action="read", resource_type="blog/authoredpost")
    ) == {str(post.pk)}
    assert list(
        backend.lookup_subjects(
            resource=post_ref,
            action="author",
            subject_type="auth/user",
        )
    ) == [alice]


def test_db_loaded_schema_preserves_field_backing(db):
    post_def = SchemaDefinition.objects.create(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=post_def,
        name="folder",
        allowed_subjects=[{"type": "blog/folder"}],
        backing={"attname": "folder", "kind": "fk"},
    )
    SchemaPermission.objects.create(
        definition=post_def,
        name="read",
        expression="folder->read",
    )
    folder_def = SchemaDefinition.objects.create(resource_type="blog/folder")
    SchemaRelation.objects.create(
        definition=folder_def,
        name="viewer",
        allowed_subjects=[{"type": "auth/user"}],
    )
    SchemaPermission.objects.create(
        definition=folder_def,
        name="read",
        expression="viewer",
    )
    SchemaDefinition.objects.create(resource_type="auth/user")

    with sudo(reason="field-backed-relations.db-loaded"):
        folder = Folder.objects.create(name="visible")
        post = Post.objects.create(title="visible", folder=folder)

    backend = LocalBackend()
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/folder", str(folder.pk)),
                relation="viewer",
                subject=_user("alice"),
            )
        ]
    )

    assert backend.has_access(subject=_user("alice"), action="read", resource=_post_ref(post))


def test_system_check_reports_missing_field_binding(db):
    from rebac.backends import LocalBackend as BackendClass
    from rebac.backends import backend as active_backend
    from rebac.backends import reset_backend
    from rebac.checks import check_field_backed_relations

    reset_backend()
    active = active_backend()
    assert isinstance(active, BackendClass)
    active.set_schema(
        parse_zed(
            """
            definition blog/folder {}
            definition blog/post {
                relation folder: blog/folder // rebac:field=missing
            }
            """
        )
    )

    issues = check_field_backed_relations()

    assert any(
        issue.id == "rebac.E009" and "missing field 'missing'" in issue.msg for issue in issues
    )
