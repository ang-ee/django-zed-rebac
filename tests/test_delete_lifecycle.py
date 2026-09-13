"""Relationship garbage collection follows both sides of Django identity."""

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest
from rebac import RelationshipTuple, SubjectRef, sudo, write_relationships
from rebac.backends import reset_backend
from rebac.models import active_relationship_model
from rebac.signals import _rebac_cascade_resource
from rebac.types import ObjectRef
from tests.testapp.models import Folder, Post


def test_delete_cleanup_uses_signal_database_alias(settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = "denormalized"
    target = Post(pk=7, title="Target")
    relationship_model = MagicMock()

    with (
        patch("rebac.models.active_relationship_model", return_value=relationship_model),
        patch("rebac.signals.transaction.atomic", return_value=nullcontext()) as atomic,
    ):
        _rebac_cascade_resource(sender=Post, instance=target, using="replica")

    atomic.assert_called_once_with(using="replica")
    relationship_model.objects.using.assert_called_once_with("replica")
    relationship_model.objects.using.return_value.filter.return_value.delete.assert_called_once_with()


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_delete_removes_resource_and_subject_occurrences(settings, storage):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
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
    assert relationships.filter(
        subject_type="blog/folder", subject_id=str(subject.pk)
    ).exists()
    reset_backend()
