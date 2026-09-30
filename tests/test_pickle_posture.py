"""Pickle posture for ``RebacMixin`` (Part J of the C+J work plan).

Per ``CLAUDE.md § 5``, instances may cross HTTP→Celery via
``apply_async(args=[instance])``. The actor's persistence across that
boundary is documented; the *sudo* bypass posture is NOT documented and
must fail closed. ``__getstate__`` strips ``_rebac_actor``,
``_rebac_sudo_reason``, and ``_rebac_loaded_values`` so a pickled
instance cannot smuggle a trusted actor across a process boundary.
"""

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

SCHEMA_TEXT = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    relation parent: blog/folder
    permission read = owner + parent->read
    permission write = owner + parent->write
}
definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user | auth/user:*
    relation folder: blog/folder
    permission read = owner + viewer + folder->read
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
def post(db, alice):
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        p = Post.objects.create(title="hello")
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(p.pk)),
                relation="owner",
                subject=SubjectRef.of("auth/user", str(alice.pk)),
            ),
        ]
    )
    return p


def test_pickle_strips_pinned_actor(alice, post):
    """An instance loaded under ``as_user`` carries ``_rebac_actor``;
    pickling and unpickling must drop it.
    """
    from tests.testapp.models import Post

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance._rebac_actor is not None  # baseline

    blob = pickle.dumps(instance)
    restored = pickle.loads(blob)

    # Fail-closed: the worker must re-attach an actor — the binding
    # never crosses the wire silently.
    assert getattr(restored, "_rebac_actor", None) is None
    assert getattr(restored, "_rebac_sudo_reason", None) is None
    # Loaded-values snapshot is also dropped — receiving worker can't
    # second-guess what the sender thought was the original row state.
    assert getattr(restored, "_rebac_loaded_values", None) is None
    # Sanity: actual row data still survived (title etc.).
    assert restored.title == "hello"
    assert restored.pk == post.pk


def test_pickle_strips_sudo_flag(alice, post):
    """Manually setting ``_rebac_sudo_reason`` does not survive pickling
    — the bypass cannot leak past a process boundary."""
    from tests.testapp.models import Post

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    # Simulate code that flipped sudo on the instance for some reason.
    instance._rebac_sudo_reason = "elevated.for.inline.fix"

    restored = pickle.loads(pickle.dumps(instance))

    assert getattr(restored, "_rebac_sudo_reason", None) is None


def test_unpickled_save_raises_missing_actor(alice, post):
    """Saving the unpickled instance under STRICT_MODE raises
    ``MissingActorError`` because the actor binding was stripped — the
    receiving worker must re-attach via middleware / a Celery hook
    before any save."""
    from tests.testapp.models import Post

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    restored = pickle.loads(pickle.dumps(instance))

    restored.title = "post-pickle update"
    with pytest.raises(MissingActorError):
        restored.save()
