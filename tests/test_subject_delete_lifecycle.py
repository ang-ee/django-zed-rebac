"""Deleted canonical Django subjects cannot leave reusable grants behind."""

from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import models
from django.test.utils import isolate_apps

from rebac import RelationshipTuple, backend, rebac_subject, sudo, write_relationships
from rebac.actors import _subject_registry, to_subject_ref
from rebac.backends import reset_backend
from rebac.models import active_relationship_model
from rebac.schema import parse_zed
from rebac.signals import _rebac_cascade_resource
from rebac.types import ObjectRef
from tests.testapp.models import Folder, Post

SCHEMA_TEXT = """
definition auth/user {}
definition auth/group {
    relation member: auth/user
}
definition blog/folder {}

definition blog/post {
    relation viewer: auth/user | auth/group#member | blog/folder
}
"""


def _delete_instance(instance: models.Model) -> None:
    instance.delete()


def _delete_queryset(instance: models.Model) -> None:
    type(instance)._base_manager.filter(pk=instance.pk).delete()


@pytest.fixture(autouse=True)
def _subject_schema(request):
    if request.node.get_closest_marker("django_db") is None:
        yield
        return
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("delete", [_delete_instance, _delete_queryset])
@pytest.mark.parametrize("subject_kind", ["user", "group", "resource"])
def test_deleting_model_subject_removes_only_its_relationships(
    settings,
    storage: str,
    delete: Callable[[models.Model], None],
    subject_kind: str,
) -> None:
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))

    user_model = get_user_model()
    with sudo(reason="subject delete lifecycle setup"):
        target = Post.objects.create(title="Target")
        if subject_kind == "user":
            deleted = user_model.objects.create_user(username="deleted")
            surviving = user_model.objects.create_user(username="surviving")
        elif subject_kind == "group":
            deleted = Group.objects.create(name="deleted")
            surviving = Group.objects.create(name="surviving")
        else:
            deleted = Folder.objects.create(name="Deleted")
            surviving = Folder.objects.create(name="Surviving")

    deleted_ref = to_subject_ref(deleted)
    surviving_ref = to_subject_ref(surviving)
    target_ref = ObjectRef("blog/post", str(target.pk))
    write_relationships(
        [
            RelationshipTuple(target_ref, "viewer", deleted_ref),
            RelationshipTuple(target_ref, "viewer", surviving_ref),
        ]
    )

    with sudo(reason="subject delete lifecycle assertion"):
        delete(deleted)

    relationships = active_relationship_model().objects
    assert not relationships.filter(
        subject_type=deleted_ref.subject_type,
        subject_id=deleted_ref.subject_id,
    ).exists()
    assert relationships.filter(
        subject_type=surviving_ref.subject_type,
        subject_id=surviving_ref.subject_id,
    ).exists()


def test_subject_cleanup_uses_signal_database_alias(settings) -> None:
    settings.REBAC_LOCAL_BACKEND_STORAGE = "denormalized"
    user = MagicMock(spec=get_user_model())
    user.pk = 7
    user.is_authenticated = True
    relationship_model = MagicMock()

    with (
        patch("rebac.models.active_relationship_model", return_value=relationship_model),
        patch("rebac.backends.local.mark_relationships_changed") as invalidated,
    ):
        _rebac_cascade_resource(sender=get_user_model(), instance=user, using="replica")

    relationship_model.objects.using.assert_called_once_with("replica")
    relationship_model.objects.using.return_value.filter.return_value.delete.assert_called_once_with()
    invalidated.assert_called_once_with()


@isolate_apps("tests")
def test_unrelated_model_delete_skips_subject_resolution() -> None:
    class Unrelated(models.Model):
        class Meta:
            app_label = "tests"

    instance = Unrelated(pk=1)
    with (
        patch("rebac.signals.to_subject_ref") as resolve_subject,
        patch("rebac.models.active_relationship_model") as relationship_model,
    ):
        _rebac_cascade_resource(sender=Unrelated, instance=instance)

    resolve_subject.assert_not_called()
    relationship_model.assert_not_called()


@isolate_apps("tests")
def test_registered_model_subject_delete_still_uses_canonical_resolver(settings) -> None:
    settings.REBAC_LOCAL_BACKEND_STORAGE = "denormalized"

    @rebac_subject(type="auth/device", id_attr="serial")
    class Device(models.Model):
        serial = models.CharField(max_length=32)

        class Meta:
            app_label = "tests"

    instance = Device(pk=1, serial="sensor-1")
    relationship_model = MagicMock()
    try:
        with (
            patch("rebac.signals.to_subject_ref", wraps=to_subject_ref) as resolve_subject,
            patch("rebac.models.active_relationship_model", return_value=relationship_model),
            patch("rebac.backends.local.mark_relationships_changed"),
        ):
            _rebac_cascade_resource(sender=Device, instance=instance)
    finally:
        _subject_registry.pop(Device, None)

    resolve_subject.assert_called_once_with(instance)
    relationship_model.objects.using.return_value.filter.return_value.delete.assert_called_once_with()
