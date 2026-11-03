"""The collector must receive the deleting caller's queryset authority."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db.models.signals import pre_delete

from rebac import (
    RelationshipTuple,
    actor_context,
    backend,
    system_context,
    to_object_ref,
    to_subject_ref,
)
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.testapp.models import Folder

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    permission read = owner
    permission delete = owner
}
"""


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("cascade", [False, True], ids=["direct", "cascade"])
@pytest.mark.parametrize("authority", ["user", "system", "pinned"])
def test_queryset_delete_signal_origin_keeps_caller_authority(cascade, authority):
    install_schema(backend(), parse_zed(SCHEMA))
    user = get_user_model().objects.create_user(username="delete-origin-user")
    subject = to_subject_ref(user)
    with system_context(reason="test.delete_origin.seed"):
        parent = Folder.objects.create(name="parent")
        child = Folder.objects.create(name="child", parent=parent)
        backend().write_relationships(
            [
                RelationshipTuple(to_object_ref(parent), "owner", subject),
                RelationshipTuple(to_object_ref(child), "owner", subject),
            ]
        )

    seen = []

    def receive(sender, instance, origin, **kwargs):
        seen.append((instance.pk, origin.effective_actor(strict=True)))

    pre_delete.connect(receive, sender=Folder, weak=False, dispatch_uid="test.delete_origin")
    try:
        target = parent if cascade else child
        if authority == "user":
            with actor_context(user):
                Folder.objects.filter(pk=target.pk).delete()
        elif authority == "system":
            with system_context(reason="test.delete_origin.delete"):
                Folder.objects.filter(pk=target.pk).delete()
        else:
            # A queryset pinned to the user keeps that authority under elevation.
            with system_context(reason="test.delete_origin.delete"):
                Folder.objects.with_actor(user).filter(pk=target.pk).delete()
    finally:
        pre_delete.disconnect(sender=Folder, dispatch_uid="test.delete_origin")

    expected_pks = {parent.pk, child.pk} if cascade else {child.pk}
    assert {pk for pk, _ in seen} == expected_pks
    expected = (None, True) if authority == "system" else (subject, False)
    assert all(seen_authority == expected for _, seen_authority in seen)
