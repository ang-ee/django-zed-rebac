from __future__ import annotations

import pytest
from django.test import override_settings

from rebac import ObjectRef, RelationshipTuple, SubjectRef, backend, sudo, to_object_ref
from rebac.backends import reset_backend
from rebac.schema import parse_zed


@pytest.fixture(autouse=True)
def _reset_backend(db):
    reset_backend()
    yield
    reset_backend()


@pytest.mark.django_db
def test_to_object_ref_uses_model_rebac_id_attr() -> None:
    from tests.testapp.models import SluggedPost

    with sudo(reason="test.fixture"):
        post = SluggedPost.objects.create(slug="public-id", title="Hello")

    assert to_object_ref(post) == ObjectRef("blog/sluggedpost", "public-id")


@pytest.mark.django_db
@override_settings(REBAC_TYPE_PREFIX="tenantA/")
def test_model_refs_managers_and_signals_share_prefixed_type_policy() -> None:
    from django.contrib.auth import get_user_model

    from tests.testapp.models import SluggedPost

    backend().set_schema(
        parse_zed(
            """
            definition auth/user {}

            definition tenantA/blog/sluggedpost {
                relation owner: auth/user
                permission read = owner
                permission write = owner
                permission delete = owner
            }
            """
        )
    )
    user = get_user_model().objects.create(username="alice", is_active=True)
    subject = SubjectRef.of("auth/user", str(user.pk))

    with sudo(reason="test.fixture"):
        post = SluggedPost.objects.create(slug="prefixed", title="Hello")

    backend().write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("tenantA/blog/sluggedpost", "prefixed"),
                relation="owner",
                subject=subject,
            )
        ]
    )

    assert to_object_ref(post) == ObjectRef("tenantA/blog/sluggedpost", "prefixed")
    assert list(SluggedPost.objects.with_actor(user).values_list("slug", flat=True)) == ["prefixed"]

    loaded = SluggedPost.objects.with_actor(user).get(slug="prefixed")
    loaded.title = "Updated"
    loaded.save()

    post.refresh_from_db()
    assert post.title == "Updated"
