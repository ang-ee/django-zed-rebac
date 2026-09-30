"""A pickled queryset crosses a trust boundary without its sudo reason or actor."""

from __future__ import annotations

import pickle

import pytest

from rebac import (
    MissingActorError,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    write_relationships,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import atomic_source_write, install_schema
from tests.testapp.models import Post

SCHEMA_TEXT = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    permission read = owner
    permission write = owner
    permission delete = owner
    permission create = owner
}
"""


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


@pytest.fixture
def alice(db):
    from django.contrib.auth import get_user_model

    return atomic_source_write(get_user_model().objects.create, username="alice", is_active=True)


@pytest.fixture
def posts(db, alice):
    with sudo(reason="test.fixture"):
        owned = Post.objects.create(title="owned")
        hidden = Post.objects.create(title="hidden")
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(owned.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(alice.pk)),
            )
        ]
    )
    return owned, hidden


def test_unpickled_sudo_queryset_has_no_bypass(posts):
    restored = pickle.loads(pickle.dumps(Post.objects.sudo(reason="pickle.sudo")))

    assert not restored.is_sudo()
    with pytest.raises(MissingActorError):
        list(restored.filter(title__isnull=False))


def test_unpickled_system_context_queryset_has_no_bypass(posts):
    restored = pickle.loads(pickle.dumps(Post.objects.system_context(reason="pickle.system")))

    assert not restored.is_sudo()
    with pytest.raises(MissingActorError):
        list(restored.filter(title__isnull=False))


def test_actor_queryset_does_not_cross_pickle_with_its_actor(alice, posts):
    queryset = Post.objects.as_user(alice)
    try:
        blob = pickle.dumps(queryset)
    except TypeError:
        # The evaluated scope holds the backend, which does not pickle.
        return
    restored = pickle.loads(blob)

    assert restored.actor() is None
    with pytest.raises(MissingActorError):
        list(restored.filter(title__isnull=False))


def test_pickled_query_attribute_does_not_carry_bypass(posts):
    queryset = Post.objects.all()
    queryset.query = pickle.loads(pickle.dumps(Post.objects.sudo(reason="pickle.query").query))

    with pytest.raises(MissingActorError):
        list(queryset)
