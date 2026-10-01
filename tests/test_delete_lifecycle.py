"""Relationship garbage collection follows both sides of Django identity."""

from unittest.mock import patch

import pytest
from django.db import connections
from django.test.utils import CaptureQueriesContext

from rebac import RelationshipTuple, SubjectRef, backend, sudo, write_relationships
from rebac.backends import reset_backend
from rebac.models import Relationship, active_relationship_model
from rebac.schema import parse_zed
from rebac.signals import _rebac_cascade_resource
from rebac.types import ObjectRef
from tests.backend_setup import install_schema, sqlite_alias
from tests.testapp.models import Folder, Post

SCHEMA_TEXT = """
definition blog/folder {}

definition blog/post {
    relation viewer: blog/folder
    relation parent: blog/post
}
"""


@pytest.fixture
def replica(db, django_db_blocker, tmp_path):
    """A second database alias holding the relationship table."""
    alias = "replica"
    target = sqlite_alias(alias, tmp_path / "replica.sqlite3")
    connections[alias] = target
    try:
        with django_db_blocker.unblock():
            with target.schema_editor() as editor:
                editor.create_model(Relationship)
            yield alias
    finally:
        target.close()
        del connections[alias]


def test_delete_cleanup_uses_signal_database_alias(settings, replica):
    settings.REBAC_LOCAL_BACKEND_STORAGE = "denormalized"
    target = Post(pk=7, title="Target")
    named = {
        "resource_type": "blog/post",
        "resource_id": "7",
        "relation": "viewer",
        "subject_type": "blog/folder",
        "subject_id": "1",
    }
    for alias in ("default", replica):
        Relationship.objects.using(alias).bulk_create([Relationship(**named)])

    with (
        patch("rebac.backends.local.mark_relationships_changed") as invalidated,
        CaptureQueriesContext(connections["default"]) as elsewhere,
        CaptureQueriesContext(connections[replica]) as queries,
    ):
        _rebac_cascade_resource(sender=Post, instance=target, using=replica)

    # The delete runs in a transaction on the signal's alias and touches no
    # other database, then drops cached decisions.
    assert not elsewhere.captured_queries
    assert any(query["sql"].startswith("DELETE") for query in queries.captured_queries)
    assert not Relationship.objects.using(replica).filter(**named).exists()
    assert Relationship.objects.using("default").filter(**named).exists()
    invalidated.assert_called()


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_delete_removes_resource_and_subject_occurrences(settings, storage):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA_TEXT))
    with sudo(reason="delete lifecycle setup"):
        subject = Folder.objects.create(name="Subject")
        target = Post.objects.create(title="Target")
    subject_ref = SubjectRef(ObjectRef("blog/folder", str(subject.pk)))
    target_ref = ObjectRef("blog/post", str(target.pk))
    write_relationships(
        [
            RelationshipTuple(target_ref, "viewer", subject_ref),
            RelationshipTuple(
                ObjectRef("blog/post", "other"),
                "parent",
                SubjectRef(target_ref),
            ),
            # Unrelated occurrence of the surviving folder subject: must remain.
            RelationshipTuple(ObjectRef("blog/post", "other"), "viewer", subject_ref),
        ]
    )

    with sudo(reason="delete lifecycle assertion"):
        target.delete()

    relationships = active_relationship_model().objects
    assert not relationships.filter(
        resource_type=target_ref.resource_type, resource_id=target_ref.resource_id
    ).exists()
    assert not relationships.filter(
        subject_type=target_ref.resource_type, subject_id=target_ref.resource_id
    ).exists()
    assert relationships.filter(subject_type="blog/folder", subject_id=str(subject.pk)).exists()
    reset_backend()
