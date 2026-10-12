"""check_save: the verdict the save gates would reach, without saving or auditing."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction

from rebac import (
    MissingActorError,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    backend,
    sudo,
    to_subject_ref,
)
from rebac.backends import reset_backend
from rebac.models import PermissionAuditEvent
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db

SCHEMA = """
definition auth/user {}

definition blog/folder {
    relation owner: auth/user
    relation item: blog/post // rebac:field=posts
    permission read = owner
    permission write = owner
}

definition blog/post {
    relation owner: auth/user
    relation editor: auth/user
    relation folder: blog/folder // rebac:field=folder
    permission read = owner + editor
    permission write = owner + editor
    permission create = folder->write
    permission write__title = owner
}
"""


@pytest.fixture(autouse=True)
def _schema(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA))
    yield
    reset_backend()


def _grant(resource_type, pk, relation, user):
    backend().write_relationships(
        [RelationshipTuple(ObjectRef(resource_type, str(pk)), relation, to_subject_ref(user))]
    )


@pytest.fixture
def world():
    users = get_user_model().objects
    alice, bob, carol = (users.create(username=name) for name in ("alice", "bob", "carol"))
    with sudo(reason="test.fixture"):
        mine = Folder.objects.create(name="mine")
        theirs = Folder.objects.create(name="theirs")
        post = Post.objects.create(title="draft", body="", folder=mine)
    _grant("blog/folder", mine.pk, "owner", alice)
    _grant("blog/folder", theirs.pk, "owner", carol)
    _grant("blog/post", post.pk, "owner", alice)
    _grant("blog/post", post.pk, "editor", bob)
    return alice, bob, carol, mine, theirs, post


def _saves(instance, **kwargs) -> bool:
    """Whether ``save()`` passes the gates; the write itself is rolled back."""
    try:
        with transaction.atomic():
            instance.save(**kwargs)
            transaction.set_rollback(True)
    except PermissionDenied:
        return False
    return True


def _loaded(user, post):
    return Post.objects.with_actor(to_subject_ref(user)).get(pk=post.pk)


def test_a_new_instance_is_checked_as_its_creation(world):
    alice, bob, _carol, mine, _theirs, _post = world
    assert Post(title="new", folder=mine).with_actor(to_subject_ref(alice)).check_save().allowed
    denied = Post(title="new", folder=mine).with_actor(to_subject_ref(bob)).check_save()
    assert not denied.allowed
    assert "create" in denied.reason
    assert not _saves(Post(title="new", folder=mine).with_actor(to_subject_ref(bob)))
    with pytest.raises(ValueError, match="update_fields"):
        Post(title="new", folder=mine).with_actor(to_subject_ref(alice)).check_save(
            update_fields=["title"]
        )


def test_a_changed_field_is_checked_as_the_save_checks_it(world):
    _alice, bob, _carol, _mine, _theirs, post = world
    edited = _loaded(bob, post)
    edited.body = "an edit"
    assert edited.check_save().allowed
    assert _saves(edited)
    retitled = _loaded(bob, post)
    retitled.title = "a new title"
    verdict = retitled.check_save()
    assert not verdict.allowed
    assert "title" in verdict.reason
    assert not _saves(retitled)
    # The gated field is not among the fields saved: the save passes.
    assert retitled.check_save(update_fields=["body"]).allowed


def test_a_moved_backed_edge_is_checked(world):
    alice, _bob, carol, _mine, theirs, post = world
    moved = _loaded(alice, post)
    moved.folder = theirs
    # Moving the post changes both folders' backed edges: alice may not write carol's.
    assert not moved.check_save().allowed
    assert not _saves(_with_folder(_loaded(alice, post), theirs))
    _grant("blog/folder", theirs.pk, "owner", alice)
    assert _with_folder(_loaded(alice, post), theirs).check_save().allowed
    del carol


def _with_folder(instance, folder):
    instance.folder = folder
    return instance


def test_nothing_is_written_and_no_denial_is_audited(world, settings):
    _alice, bob, _carol, _mine, _theirs, post = world
    settings.REBAC_AUDIT_DENIALS = True
    before = PermissionAuditEvent.objects.count()
    retitled = _loaded(bob, post)
    retitled.title = "a new title"
    assert not retitled.check_save().allowed
    edited = _loaded(bob, post)
    edited.body = "an edit"
    assert edited.check_save().allowed
    assert PermissionAuditEvent.objects.count() == before
    with sudo(reason="test.verify"):
        stored = Post.objects.get(pk=post.pk)
    assert (stored.title, stored.body) == ("draft", "")
    # The save itself still audits its denial.
    assert not _saves(retitled)
    assert PermissionAuditEvent.objects.count() == before + 1


def test_the_actor_is_resolved_as_the_save_resolves_it(world):
    _alice, bob, _carol, _mine, _theirs, post = world
    retitled = _loaded(bob, post)
    retitled.title = "a new title"
    # A pinned actor outranks ambient sudo; an instance bypass passes.
    with sudo(reason="test.ambient"):
        assert not retitled.check_save().allowed
    assert retitled.sudo(reason="test.instance").check_save().allowed
    with sudo(reason="test.fixture"):
        unpinned = Post.objects.get(pk=post.pk)
    unpinned.title = "a new title"
    with pytest.raises(MissingActorError):
        unpinned.check_save()
