"""Lazy queryset scope composes live attribute-backed membership."""

import pytest

from rebac import (
    ObjectRef,
    RelationshipTuple,
    backend,
    sudo,
    to_object_ref,
    to_subject_ref,
    write_relationships,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.testapp.models import AuthoredPost, Folder, Post


SCHEMA = """
definition auth/user {}

definition platform/role {
    relation member: auth/user // rebac:attribute={"field":"is_superuser","resource":"admin","value":true,"filters":{"is_active":true}}
    permission effective_member = member
}

definition blog/post {
    relation admin: platform/role // rebac:const=admin
    permission read = admin->effective_member
}
"""


@pytest.mark.django_db
def test_fixed_attribute_anchor_is_lazy_and_honours_filters(django_user_model):
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA))
    actor = django_user_model.objects.create(
        username="admin",
        is_active=True,
        is_superuser=True,
    )
    with sudo(reason="test.fixture"):
        first = Post.objects.create(title="First")
        second = Post.objects.create(title="Second")

    scoped = Post.objects.with_actor(actor).order_by("pk")
    assert list(scoped) == [first, second]

    pending = Post.objects.with_actor(actor)
    actor.is_active = False
    actor.save(update_fields=["is_active"])
    assert list(pending) == []


@pytest.mark.django_db
def test_attribute_anchor_preserves_unmatched_resource_tuple(django_user_model):
    reset_backend()
    backend().set_schema(
        parse_zed(SCHEMA.replace("rebac:const=admin", "rebac:const=editor"))
    )
    actor = django_user_model.objects.create(
        username="admin",
        is_active=True,
        is_superuser=True,
    )
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="Tuple-backed editor")
        write_relationships(
            [
                RelationshipTuple(
                    ObjectRef("platform/role", "editor"),
                    "member",
                    to_subject_ref(actor),
                )
            ]
        )

    assert list(Post.objects.with_actor(actor)) == [post]


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_filtered_reverse_path_uses_one_join_and_stays_lazy(
    django_user_model, settings, storage
):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    backend().set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/folder {
                relation member: auth/user // rebac:field={"path":"authored_posts__author","filters":{"authored_posts__title":"allowed"}}
                permission read = member
            }
            """
        )
    )
    alice = django_user_model.objects.create(username="alice", is_active=True)
    bob = django_user_model.objects.create(username="bob", is_active=True)
    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="Shared")
        AuthoredPost.objects.create(title="denied", folder=folder, author=alice)
        allowed = AuthoredPost.objects.create(title="allowed", folder=folder, author=bob)

    resource = to_object_ref(folder)
    assert not backend().check_access(
        subject=to_subject_ref(alice), action="read", resource=resource
    ).allowed
    assert backend().check_access(
        subject=to_subject_ref(bob), action="read", resource=resource
    ).allowed
    assert set(
        backend().accessible(
            subject=to_subject_ref(alice), action="read", resource_type="blog/folder"
        )
    ) == set()
    assert set(
        backend().accessible(
            subject=to_subject_ref(bob), action="read", resource_type="blog/folder"
        )
    ) == {str(folder.pk)}
    assert list(Folder.objects.with_actor(alice)) == []
    assert list(Folder.objects.with_actor(bob)) == [folder]

    pending = Folder.objects.with_actor(bob)
    with sudo(reason="test.fixture"):
        allowed.title = "revoked"
        allowed.save(update_fields=["title"])
    assert not backend().check_access(
        subject=to_subject_ref(bob), action="read", resource=resource
    ).allowed
    assert set(
        backend().accessible(
            subject=to_subject_ref(bob), action="read", resource_type="blog/folder"
        )
    ) == set()
    assert list(pending) == []
