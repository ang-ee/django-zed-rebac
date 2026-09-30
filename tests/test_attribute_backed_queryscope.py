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
from tests.backend_setup import atomic_source_write, install_schema
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
    install_schema(backend(), parse_zed(SCHEMA))
    actor = atomic_source_write(
        django_user_model.objects.create,
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
    atomic_source_write(actor.save, update_fields=["is_active"])
    assert list(pending) == []


@pytest.mark.django_db
def test_attribute_anchor_preserves_unmatched_resource_tuple(django_user_model):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA.replace("rebac:const=admin", "rebac:const=editor")))
    actor = atomic_source_write(
        django_user_model.objects.create,
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
def test_filtered_reverse_path_uses_one_join_and_stays_lazy(django_user_model, settings, storage):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    install_schema(
        backend(),
        parse_zed(
            """
            definition auth/user {}
            definition blog/folder {
                relation member: auth/user // rebac:field={"path":"authored_posts__author","filters":{"authored_posts__title":"allowed"}}
                permission read = member
            }
            """
        ),
    )
    alice = atomic_source_write(django_user_model.objects.create, username="alice", is_active=True)
    bob = atomic_source_write(django_user_model.objects.create, username="bob", is_active=True)
    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="Shared")
        AuthoredPost.objects.create(title="denied", folder=folder, author=alice)
        allowed = AuthoredPost.objects.create(title="allowed", folder=folder, author=bob)

    resource = to_object_ref(folder)
    assert (
        not backend()
        .check_access(subject=to_subject_ref(alice), action="read", resource=resource)
        .allowed
    )
    assert (
        backend()
        .check_access(subject=to_subject_ref(bob), action="read", resource=resource)
        .allowed
    )
    assert (
        set(
            backend().accessible(
                subject=to_subject_ref(alice), action="read", resource_type="blog/folder"
            )
        )
        == set()
    )
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
    assert (
        not backend()
        .check_access(subject=to_subject_ref(bob), action="read", resource=resource)
        .allowed
    )
    assert (
        set(
            backend().accessible(
                subject=to_subject_ref(bob), action="read", resource_type="blog/folder"
            )
        )
        == set()
    )
    assert list(pending) == []


CONTAINER_SCHEMA = """
definition blog/folder {}

definition blog/sluggedpost {
    relation member: blog/folder // rebac:attribute={"field":"%s"}
    permission read = member
}
"""


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "expect_sql"),
    [("name", True), ("kind", False)],
    ids=["stock-charfield", "transforming-field-refused"],
)
def test_dynamic_container_parity_holds_for_noncanonical_stored_values(field, expect_sql):
    """Direct check, ``accessible()`` and the scoped queryset agree on membership.

    Stock string fields retain case-sensitive membership. A custom Python
    conversion that SQL cannot reproduce is explicitly refused by E014.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from tests.testapp.models import SluggedPost

    reset_backend()
    if not expect_sql:
        from rebac.errors import SchemaError
        from rebac.index.codec import identity_codec

        with pytest.raises(SchemaError, match=r"rebac\.E014"):
            identity_codec(Folder, field)
        return
    install_schema(backend(), parse_zed(CONTAINER_SCHEMA % field))
    with sudo(reason="test.fixture"):
        actor = Folder.objects.create(name="Premium", kind="Premium")
        canonical = SluggedPost.objects.create(slug="premium", title="lower")
        SluggedPost.objects.create(slug="Premium", title="upper")
    actor.refresh_from_db()
    stored = getattr(actor, field)  # "Premium" for name, "premium" for kind
    subject = to_subject_ref(actor)

    expected = {stored} if field == "name" else {"premium"}
    assert (
        set(backend().accessible(subject=subject, action="read", resource_type="blog/sluggedpost"))
        == expected
    )
    for slug in ("premium", "Premium"):
        assert backend().check_access(
            subject=subject, action="read", resource=ObjectRef("blog/sluggedpost", slug)
        ).allowed is (slug in expected)
    assert {post.slug for post in SluggedPost.objects.with_actor(actor)} == expected

    # A repeated lazy scope retains the same membership.
    with CaptureQueriesContext(connection):
        assert {post.slug for post in SluggedPost.objects.with_actor(actor)} == expected
    del canonical


ARROW_SCHEMA = """
definition auth/user {
    relation self: auth/user
    permission reach = self
}

definition test/kind {
    relation member: auth/user // rebac:attribute={"field":"is_staff","resource":"staff","value":true,"filters":{"authored_test_posts__title__startswith":"k"}}
    permission reach = member->reach
}

definition blog/post {
    relation kind: test/kind // rebac:const=staff
    permission read = kind->reach
}
"""


@pytest.mark.django_db
def test_direct_live_checks_use_witness_and_one_index_query(
    django_user_model, django_assert_num_queries
):
    reset_backend()
    install_schema(
        backend(),
        parse_zed(
            """
            definition auth/user {}
            definition blog/folder {
                relation member: auth/user // rebac:field={"path":"authored_posts__author","filters":{"authored_posts__title":"allowed"}}
                relation staff: auth/user // rebac:attribute={"field":"is_staff","resource":"admin","value":true}
                permission read = member
                permission manage = staff
            }
            """
        ),
    )
    alice = atomic_source_write(
        django_user_model.objects.create, username="alice", is_active=True, is_staff=True
    )
    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="Shared")
        AuthoredPost.objects.create(title="allowed", folder=folder, author=alice)
    subject = to_subject_ref(alice)

    with django_assert_num_queries(2):
        assert (
            backend()
            .check_access(subject=subject, action="read", resource=to_object_ref(folder))
            .allowed
        )
    with django_assert_num_queries(2):
        assert (
            backend()
            .check_access(
                subject=subject, action="manage", resource=ObjectRef("blog/folder", "admin")
            )
            .allowed
        )


CAVEATED_ARROW_SCHEMA = ARROW_SCHEMA.replace(
    "definition auth/user {\n    relation self: auth/user",
    'caveat present(token string) {\n    token == "x"\n}\n\n'
    "definition auth/user {\n    relation self: auth/user | auth/user with present",
)


@pytest.mark.django_db
def test_attribute_arrow_preserves_caveats_in_one_index_query(
    django_user_model, django_assert_num_queries
):
    """Caveated arrows preserve tri-state results without per-target reads."""
    reset_backend()
    install_schema(backend(), parse_zed(CAVEATED_ARROW_SCHEMA))
    reader = atomic_source_write(
        django_user_model.objects.create, username="reader", is_active=True
    )
    staff = [
        atomic_source_write(
            django_user_model.objects.create, username=f"staff{i}", is_active=True, is_staff=True
        )
        for i in range(2)
    ]
    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="Shared")
        for member in staff:
            AuthoredPost.objects.create(title="k-one", folder=folder, author=member)
            AuthoredPost.objects.create(title="k-two", folder=folder, author=member)
    subject = to_subject_ref(reader)

    with django_assert_num_queries(2):
        assert (
            not backend()
            .check_access(subject=subject, action="read", resource=ObjectRef("blog/post", "one"))
            .allowed
        )

    from rebac import PermissionResult

    write_relationships(
        [
            RelationshipTuple(
                ObjectRef("auth/user", str(staff[0].pk)), "self", subject, caveat_name="present"
            ),
        ]
    )
    with django_assert_num_queries(2):
        conditional = backend().check_access(
            subject=subject, action="read", resource=ObjectRef("blog/post", "one")
        )
    assert conditional.result == PermissionResult.CONDITIONAL_PERMISSION
    assert conditional.conditional_on == ("token",)
    assert (
        backend()
        .check_access(
            subject=subject,
            action="read",
            resource=ObjectRef("blog/post", "one"),
            context={"token": "x"},
        )
        .allowed
    )
    assert (
        not backend()
        .check_access(
            subject=subject,
            action="read",
            resource=ObjectRef("blog/post", "one"),
            context={"token": "wrong"},
        )
        .allowed
    )
