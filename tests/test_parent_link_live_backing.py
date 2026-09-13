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
from tests.testapp.models import (
    NativeParentLinkedChild,
    NativeParentLinkedRecord,
    ParentLinkedChild,
    ParentLinkedRecord,
)

pytestmark = pytest.mark.django_db(transaction=True)

SCHEMA = """
definition auth/user {}

definition test/parentlinkedchild {
    relation owner: auth/user // rebac:field=owner
    relation viewer: auth/user
    permission read = owner + viewer
}

definition test/parentlinkedrecord {
    relation child: test/parentlinkedchild // rebac:field=child
    permission read = child->read
}

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
        local.set_schema(parse_zed(SCHEMA))
        try:
            yield local
        finally:
            reset_backend()


def test_parent_link_child_is_live_source_with_encoded_pk(active, django_user_model):
    alice = django_user_model.objects.create_user(username="alice")
    bob = django_user_model.objects.create_user(username="bob")
    with sudo(reason="parent-link source fixtures"):
        child = ParentLinkedChild.objects.create(id="item-41", name="child", owner=alice)
        other = ParentLinkedChild.objects.create(id="item-42", name="other", owner=bob)
    alice_ref = to_subject_ref(alice)

    assert child.pk == "item-41"
    assert to_object_ref(child).resource_id == "item-41"
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
        active.accessible(subject=alice_ref, action="read", resource_type="test/parentlinkedchild")
    ) == {"item-41"}

    with patch.object(active, "accessible", side_effect=AssertionError("enumerated direct FK")):
        assert list(ParentLinkedChild.objects.with_actor(alice)) == [child]
    assert list(ParentLinkedChild.objects.with_actor(bob)) == [other]


def test_parent_link_child_is_live_target_for_direct_arrow_and_lazy_scope(
    active, django_user_model
):
    alice = django_user_model.objects.create_user(username="alice")
    bob = django_user_model.objects.create_user(username="bob")
    with sudo(reason="parent-link target fixtures"):
        child = ParentLinkedChild.objects.create(id="item-51", name="child", owner=alice)
        record = ParentLinkedRecord.objects.create(child=child)
    child_subject = SubjectRef.of("test/parentlinkedchild", "item-51")

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
            subject_type="test/parentlinkedchild",
        )
    ) == [child_subject]
    assert set(
        active.accessible(
            subject=to_subject_ref(alice),
            action="read",
            resource_type="test/parentlinkedrecord",
        )
    ) == {str(record.pk)}

    with patch.object(active, "accessible", side_effect=AssertionError("enumerated MTI arrow")):
        assert list(ParentLinkedRecord.objects.with_actor(alice)) == [record]

    pending = ParentLinkedRecord.objects.with_actor(alice)
    with sudo(reason="transfer parent-link owner fixture"):
        child.owner = bob
        child.save(update_fields=["owner"])
    assert list(pending) == []
    assert list(ParentLinkedRecord.objects.with_actor(bob)) == [record]


def test_native_parent_link_child_and_arrow_scopes_compile_without_enumeration(
    active, django_user_model
):
    alice = django_user_model.objects.create_user(username="alice")
    bob = django_user_model.objects.create_user(username="bob")
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
    [(NativeParentLinkedChild, None), (ParentLinkedChild, "item-61")],
    ids=["native-parent-link", "encoded-parent-link"],
)
def test_parent_link_stored_viewer_union_stays_lazy_through_revocation(
    active, django_user_model, model, identity
):
    alice = django_user_model.objects.create_user(username=f"alice-{model.__name__}")
    bob = django_user_model.objects.create_user(username=f"bob-{model.__name__}")
    charlie = django_user_model.objects.create_user(username=f"charlie-{model.__name__}")
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
