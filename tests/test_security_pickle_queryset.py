"""A pickled queryset crosses a trust boundary without its sudo reason or actor."""

from __future__ import annotations

import pickle

import pytest
from django.test import override_settings

from rebac import (
    MissingActorError,
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    actor_context,
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


def test_pickle_does_not_evaluate_queryset(posts, django_assert_num_queries):
    with django_assert_num_queries(0):
        pickle.dumps(Post.objects.all())


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_evaluated_queryset_is_redacted_after_unpickling():
    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition blog/post {
            relation owner: auth/user
            relation editor: auth/user
            permission read = owner + editor
            permission read__body = owner
        }
        """),
    )
    editor = SubjectRef.of("auth/user", "editor")
    with sudo(reason="fixture"):
        post = Post.objects.create(title="public", body="secret body")
    write_relationships([RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "editor", editor)])
    rows = Post.objects.with_actor(editor)
    assert next(iter(rows)).body in (None, "")
    restored = pickle.loads(pickle.dumps(rows))
    with actor_context(editor):
        assert next(iter(restored)).body in (None, "")
