from __future__ import annotations

from types import SimpleNamespace

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef, actor_context, backend, sudo
from rebac.backends import reset_backend
from rebac.drf import RebacFilterBackend, RebacPermission
from rebac.schema import parse_zed

SCHEMA_TEXT = """
definition auth/user {}

definition agents/grant {}

definition blog/post {
    relation owner: auth/user | agents/grant#valid
    permission read = owner
    permission write = owner
    permission delete = owner
}
"""


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


@pytest.mark.django_db
def test_drf_prefers_current_actor_over_request_user_for_permissions_and_filtering() -> None:
    from django.contrib.auth import get_user_model

    from tests.testapp.models import Post

    alice = get_user_model().objects.create(username="alice", is_active=True)
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="Visible to Alice")
    backend().write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(alice.pk)),
            )
        ]
    )

    request = SimpleNamespace(method="GET", user=alice)
    view = SimpleNamespace(action="list", queryset=Post.objects.all())
    grant_actor = SubjectRef.of("agents/grant", "g1", "valid")

    with actor_context(grant_actor):
        assert not RebacPermission().has_permission(request, view)
        assert not RebacPermission().has_object_permission(request, view, post)
        scoped = RebacFilterBackend().filter_queryset(request, Post.objects.all(), view)
        assert list(scoped) == []
