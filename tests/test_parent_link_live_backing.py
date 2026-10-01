"""Multi-table parent-link identities remain valid live-backing scalars."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings

from rebac import (
    LocalBackend,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    to_object_ref,
    to_subject_ref,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import atomic_source_write, install_schema
from tests.testapp.models import (
    NativeParentLinkedChild,
    NativeParentLinkedRecord,
)

pytestmark = pytest.mark.django_db(transaction=True)

SCHEMA = """
definition auth/user {}

definition test/nativeparentlinkedchild {
    relation owner: auth/user // rebac:field=owner
    relation viewer: auth/user
    permission read = owner + viewer
}

definition test/nativeparentlinkedrecord {
    relation child: test/nativeparentlinkedchild // rebac:field=child
    permission read = child->read
}
"""


@pytest.fixture(params=["denormalized", "registry"])
def active(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        local = backend()
        assert isinstance(local, LocalBackend)
        install_schema(local, parse_zed(SCHEMA))
        try:
            yield local
        finally:
            reset_backend()


def test_parent_link_child_is_live_source_with_encoded_pk(active, django_user_model):
    alice = atomic_source_write(django_user_model.objects.create_user, username="alice")
    bob = atomic_source_write(django_user_model.objects.create_user, username="bob")
    with sudo(reason="parent-link source fixtures"):
        child = NativeParentLinkedChild.objects.create(id=41, name="child", owner=alice)
        other = NativeParentLinkedChild.objects.create(id=42, name="other", owner=bob)
    alice_ref = to_subject_ref(alice)

    assert child.pk == 41
    assert to_object_ref(child).resource_id == "41"
    assert active.has_access(subject=alice_ref, action="owner", resource=to_object_ref(child))
    assert not active.has_access(
        subject=to_subject_ref(bob), action="owner", resource=to_object_ref(child)
    )
    assert list(
        active.lookup_subjects(
            resource=to_object_ref(child), action="owner", subject_type="auth/user"
        )
    ) == [alice_ref]
    assert set(
        active.accessible(
            subject=alice_ref, action="read", resource_type="test/nativeparentlinkedchild"
        )
    ) == {"41"}

    with patch.object(active, "accessible", side_effect=AssertionError("enumerated direct FK")):
        assert list(NativeParentLinkedChild.objects.with_actor(alice)) == [child]
    assert list(NativeParentLinkedChild.objects.with_actor(bob)) == [other]


def test_parent_link_child_is_live_target_for_direct_arrow_and_lazy_scope(
    active, django_user_model
):
    alice = atomic_source_write(django_user_model.objects.create_user, username="alice")
    bob = atomic_source_write(django_user_model.objects.create_user, username="bob")
    with sudo(reason="parent-link target fixtures"):
        child = NativeParentLinkedChild.objects.create(id=51, name="child", owner=alice)
        record = NativeParentLinkedRecord.objects.create(child=child)
    child_subject = SubjectRef.of("test/nativeparentlinkedchild", "51")

    assert active.has_access(subject=child_subject, action="child", resource=to_object_ref(record))
    assert active.has_access(
        subject=to_subject_ref(alice), action="read", resource=to_object_ref(record)
    )
    assert not active.has_access(
        subject=to_subject_ref(bob), action="read", resource=to_object_ref(record)
    )
    assert list(
        active.lookup_subjects(
            resource=to_object_ref(record),
            action="child",
            subject_type="test/nativeparentlinkedchild",
        )
    ) == [child_subject]
    assert set(
        active.accessible(
            subject=to_subject_ref(alice),
            action="read",
            resource_type="test/nativeparentlinkedrecord",
        )
    ) == {str(record.pk)}

    with patch.object(active, "accessible", side_effect=AssertionError("enumerated MTI arrow")):
        assert list(NativeParentLinkedRecord.objects.with_actor(alice)) == [record]

    pending = NativeParentLinkedRecord.objects.with_actor(alice)
    with sudo(reason="transfer parent-link owner fixture"):
        child.owner = bob
        child.save(update_fields=["owner"])
    assert list(pending) == []
    assert list(NativeParentLinkedRecord.objects.with_actor(bob)) == [record]


def test_native_parent_link_child_and_arrow_scopes_compile_without_enumeration(
    active, django_user_model
):
    alice = atomic_source_write(django_user_model.objects.create_user, username="alice")
    bob = atomic_source_write(django_user_model.objects.create_user, username="bob")
    with sudo(reason="native parent-link fixtures"):
        child = NativeParentLinkedChild.objects.create(name="child", owner=alice)
        record = NativeParentLinkedRecord.objects.create(child=child)
    child_subject = SubjectRef.of("test/nativeparentlinkedchild", str(child.pk))

    assert active.has_access(subject=child_subject, action="child", resource=to_object_ref(record))
    assert active.has_access(
        subject=to_subject_ref(alice), action="read", resource=to_object_ref(record)
    )
    assert not active.has_access(
        subject=to_subject_ref(bob), action="read", resource=to_object_ref(record)
    )
    assert list(
        active.lookup_subjects(
            resource=to_object_ref(record),
            action="child",
            subject_type="test/nativeparentlinkedchild",
        )
    ) == [child_subject]

    with patch.object(active, "accessible", side_effect=AssertionError("enumerated native MTI")):
        assert list(NativeParentLinkedChild.objects.with_actor(alice)) == [child]
        assert list(NativeParentLinkedRecord.objects.with_actor(alice)) == [record]


@pytest.mark.parametrize(
    ("model", "identity"),
    [(NativeParentLinkedChild, None), (NativeParentLinkedChild, 61)],
    ids=["automatic-parent-id", "explicit-parent-id"],
)
def test_parent_link_stored_viewer_union_stays_lazy_through_revocation(
    active, django_user_model, model, identity
):
    alice = atomic_source_write(
        django_user_model.objects.create_user, username=f"alice-{model.__name__}"
    )
    bob = atomic_source_write(
        django_user_model.objects.create_user, username=f"bob-{model.__name__}"
    )
    charlie = atomic_source_write(
        django_user_model.objects.create_user, username=f"charlie-{model.__name__}"
    )
    fields = {"name": "shared", "owner": alice}
    if identity is not None:
        fields["id"] = identity
    with sudo(reason="parent-link stored viewer fixtures"):
        child = model.objects.create(**fields)
    viewer = RelationshipTuple(to_object_ref(child), "viewer", to_subject_ref(charlie))
    active.write_relationships([viewer])

    with patch.object(active, "accessible", side_effect=AssertionError("enumerated MTI tuples")):
        assert list(model.objects.with_actor(alice)) == [child]
        assert list(model.objects.with_actor(charlie)) == [child]
        assert list(model.objects.with_actor(bob)) == []
        pending = model.objects.with_actor(charlie)
        active.delete_relationship(viewer)
        assert list(pending) == []


@pytest.mark.parametrize("model_name", ["ParentLinkedChild", "ParentLinkedResource"])
def test_encoded_parent_link_identity_is_refused(model_name):
    from rebac.codec import identity_codec
    from rebac.errors import SchemaError
    from tests.testapp import models

    with pytest.raises(SchemaError, match=r"rebac\.E014"):
        identity_codec(getattr(models, model_name))
