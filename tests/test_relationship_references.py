"""Native stored tuple selection uses one identity contract in both storage shapes."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType

from rebac import ObjectRef, RelationshipTuple, SubjectRef, backend, model_identities, write_relationships
from rebac.backends import reset_backend
from rebac.models import active_relationship_model
from rebac.schema import parse_zed
from rebac.signals import cleanup_identities
from tests.backend_setup import install_schema
from tests.testapp.models import Post


def test_native_model_view_resolves_subject_resource_and_untyped_identities():
    user = get_user_model()(pk=7, username="identity-view")
    assert model_identities(type(user), user) == {ObjectRef("auth/user", "7")}
    row = Post(pk=11, title="identity-view")
    assert model_identities(Post, row) == {ObjectRef("blog/post", "11")}
    assert model_identities(ContentType, ContentType(pk=7, app_label="plain", model="row")) == set()


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ("denormalized", "registry"))
def test_reference_query_and_cleanup_share_both_ends_without_identity_collisions(settings, storage):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    install_schema(backend(), parse_zed("""
        definition auth/user {}
        definition auth/group {}
        definition blog/post {
            relation viewer: auth/user | auth/group
        }
    """))
    outgoing = RelationshipTuple(ObjectRef("blog/post", "7"), "viewer", SubjectRef.of("auth/user", "11"))
    incoming = RelationshipTuple(ObjectRef("blog/post", "99"), "viewer", SubjectRef.of("auth/user", "7"))
    same_id_other_type = RelationshipTuple(
        ObjectRef("blog/post", "99"), "viewer", SubjectRef.of("auth/group", "7"),
    )
    unrelated = RelationshipTuple(ObjectRef("blog/post", "11"), "viewer", SubjectRef.of("auth/user", "11"))
    write_relationships((outgoing, incoming, same_id_other_type, unrelated))
    rows = active_relationship_model().objects.using("default")
    identities = (ObjectRef("blog/post", "7"), ObjectRef("auth/user", "7"))

    assert rows.referencing(identities).count() == 2
    assert rows.filter(subject_id="11").referencing(identities).count() == 1
    assert not rows.referencing(()).exists()
    assert rows.referencing((ObjectRef("auth/user", "missing"),)).count() == 0

    cleanup_identities(identities, using="default")
    assert not rows.referencing(identities).exists()
    assert rows.filter(subject_type="auth/group", subject_id="7").exists()
    assert rows.filter(resource_type="blog/post", resource_id="11", subject_id="11").exists()
