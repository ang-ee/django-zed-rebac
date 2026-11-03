"""check_save: a direct verdict over the current instance before a real save."""

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


def _agree(instance, **kwargs) -> bool:
    """check_save()'s verdict, asserted to be save()'s."""
    verdict = instance.check_save(**kwargs).allowed
    assert verdict == _saves(instance, **kwargs)
    return verdict


def _loaded(user, post, *fields):
    rows = Post.objects.with_actor(to_subject_ref(user))
    return (rows.only(*fields) if fields else rows).get(pk=post.pk)


def _denials():
    return PermissionAuditEvent.objects.filter(reason__startswith="denied:").count()


def test_a_new_instance_is_checked_as_its_creation(world):
    alice, bob, _carol, mine, _theirs, _post = world
    assert _agree(Post(title="new", folder=mine).with_actor(to_subject_ref(alice)))
    denied = Post(title="new", folder=mine).with_actor(to_subject_ref(bob))
    assert not _agree(denied)
    assert "create" in denied.check_save().reason
    # As save() does, a new instance refuses update_fields.
    with pytest.raises(ValueError, match="update_fields"):
        Post(title="new", folder=mine).with_actor(to_subject_ref(alice)).check_save(
            update_fields=["title"]
        )


def test_a_changed_field_is_checked_as_the_save_checks_it(world):
    _alice, bob, _carol, _mine, _theirs, post = world
    edited = _loaded(bob, post)
    edited.body = "an edit"
    assert _agree(edited)
    retitled = _loaded(bob, post)
    retitled.title = "a new title"
    assert not _agree(retitled)
    assert "title" in retitled.check_save().reason
    # The gated field is not among the fields saved, or nothing is saved.
    assert _agree(retitled, update_fields=["body"])
    assert _agree(retitled, update_fields=[])


def test_a_redacted_field_is_left_unwritten_and_named_by_denied_read_fields(world, settings):
    """The check passes without the redacted field: a caller validating after it
    excludes what ``denied_read_fields()`` names."""
    settings.REBAC_FIELD_READ_MODE = "redact"
    install_schema(
        backend(),
        parse_zed(
            SCHEMA.replace(
                "    permission write__title = owner\n",
                "    permission write__title = owner\n    permission read__title = owner\n",
            )
        ),
    )
    _alice, bob, _carol, _mine, _theirs, post = world
    edited = _loaded(bob, post)
    assert edited.title is None
    edited.title = "a title bob can neither read nor write"
    edited.body = "an edit"
    assert _agree(edited)
    assert edited.denied_read_fields() == {"title"}
    saved = _loaded(bob, post)
    saved.title = "a title bob can neither read nor write"
    saved.body = "an edit"
    saved.save()
    with sudo(reason="test.verify"):
        stored = Post.objects.get(pk=post.pk)
    assert (stored.title, stored.body) == ("draft", "an edit")


def test_a_deferred_instance_is_checked_on_its_loaded_fields(world):
    _alice, bob, _carol, _mine, _theirs, post = world
    edited = _loaded(bob, post, "body")
    edited.body = "an edit"
    deferred = edited.get_deferred_fields()
    assert "title" in deferred
    assert _agree(edited)
    # The check loads nothing the save would not.
    assert edited.get_deferred_fields() == deferred


def test_all_non_key_fields_deferred_still_checks_full_save(world):
    _alice, _bob, carol, _mine, _theirs, post = world
    with sudo(reason="test.fixture"):
        instance = Post.objects.only("pk").get(pk=post.pk)
    instance.with_actor(to_subject_ref(carol))
    assert not instance.check_save().allowed
    with pytest.raises(PermissionDenied):
        instance.save()


def test_check_save_rejects_unknown_update_fields(world):
    alice, _bob, _carol, _mine, _theirs, post = world
    instance = _loaded(alice, post)
    with pytest.raises(ValueError, match="do not exist"):
        instance.check_save(update_fields=["not_a_field"])


def _moved(user, post, folder):
    instance = _loaded(user, post)
    instance.folder = folder
    return instance


def test_a_moved_backed_edge_is_checked(world):
    alice, _bob, _carol, _mine, theirs, post = world
    # Moving the post changes both folders' backed edges: alice may not write carol's.
    assert not _agree(_moved(alice, post, theirs))
    _grant("blog/folder", theirs.pk, "owner", alice)
    assert _agree(_moved(alice, post, theirs))


def test_a_related_object_saved_after_it_was_assigned_is_checked(world):
    alice, _bob, carol, _mine, _theirs, post = world
    moved = _loaded(alice, post)
    elsewhere = Folder(name="elsewhere")
    moved.folder = elsewhere
    with sudo(reason="test.fixture"):
        elsewhere.save()
    _grant("blog/folder", elsewhere.pk, "owner", carol)
    # save() takes the folder's key now and moves the post into carol's folder.
    assert not _agree(moved)


def test_the_models_save_override_runs_only_at_the_real_save(world, monkeypatch):
    alice, _bob, _carol, _mine, theirs, post = world
    original = Post.save

    def save(self, *args, **kwargs):
        self.folder = theirs
        original(self, *args, **kwargs)

    monkeypatch.setattr(Post, "save", save)
    edited = _loaded(alice, post)
    edited.body = "an edit"
    # The direct check sees the current instance; the real save gates the override's change.
    assert edited.check_save().allowed
    with pytest.raises(PermissionDenied):
        edited.save()


def test_nothing_is_written_and_no_denial_is_audited(world, settings):
    _alice, bob, _carol, _mine, _theirs, post = world
    settings.REBAC_AUDIT_DENIALS = True
    before = _denials()
    retitled = _loaded(bob, post)
    retitled.title = "a new title"
    assert not retitled.check_save().allowed
    edited = _loaded(bob, post)
    edited.body = "an edit"
    assert edited.check_save().allowed
    assert _denials() == before
    assert Post._base_manager.filter(pk=post.pk, title="draft", body="").exists()
    # The save itself audits its denial.
    with pytest.raises(PermissionDenied):
        retitled.save()
    assert _denials() == before + 1
    assert PermissionAuditEvent.objects.get(reason__startswith="denied:").target_repr == (
        f"blog/post:{post.pk}#write__title"
    )


def test_an_instance_bypass_is_audited_once(world):
    _alice, bob, _carol, _mine, _theirs, post = world
    bypasses = PermissionAuditEvent.objects.filter(kind=PermissionAuditEvent.KIND_SUDO_BYPASS)
    retitled = _loaded(bob, post).sudo(reason="test.instance")
    retitled.title = "a new title"
    before = bypasses.count()
    assert retitled.check_save().allowed
    retitled.save()
    # The check took the bypass and audited it; the save does not audit it again.
    assert bypasses.count() == before + 1


def test_the_actor_is_resolved_as_the_save_resolves_it(world):
    _alice, bob, _carol, _mine, _theirs, post = world
    retitled = _loaded(bob, post)
    retitled.title = "a new title"
    # A pinned actor outranks ambient sudo.
    with sudo(reason="test.ambient"):
        assert not retitled.check_save().allowed
    with sudo(reason="test.fixture"):
        unpinned = Post.objects.get(pk=post.pk)
    unpinned.title = "a new title"
    with pytest.raises(MissingActorError):
        unpinned.check_save()


def test_a_check_from_within_its_own_save_does_not_recurse(world, monkeypatch):
    _alice, bob, _carol, _mine, _theirs, post = world
    original = Post.save

    def save(self, *args, **kwargs):
        if not self.check_save().allowed:
            raise PermissionDenied("refused by the override")
        original(self, *args, **kwargs)

    monkeypatch.setattr(Post, "save", save)
    retitled = _loaded(bob, post)
    retitled.title = "a new title"
    assert not _agree(retitled)
    edited = _loaded(bob, post)
    edited.body = "an edit"
    assert _agree(edited)


def test_check_save_does_not_run_an_override_that_creates_related_rows(world, monkeypatch):
    alice, _bob, _carol, _mine, _theirs, _post = world
    original = Post.save

    def save(self, *args, **kwargs):
        if self.folder_id is None:
            with sudo(reason="test.override"):
                self.folder = Folder.objects.create(name="made by the override")
            _grant("blog/folder", self.folder.pk, "owner", alice)
        original(self, *args, **kwargs)

    monkeypatch.setattr(Post, "save", save)
    candidate = Post(title="new").with_actor(to_subject_ref(alice))
    assert not candidate.check_save().allowed
    # No override ran during the direct gate check.
    assert candidate.folder_id is None
    assert candidate._state.adding
    candidate.save()
    assert Folder._base_manager.filter(pk=candidate.folder_id).exists()


def test_a_bypass_in_the_save_override_is_audited_by_the_save(world, monkeypatch):
    alice, _bob, _carol, _mine, _theirs, post = world
    original = Post.save

    def save(self, *args, **kwargs):
        self.folder.save()
        original(self, *args, **kwargs)

    monkeypatch.setattr(Post, "save", save)
    edited = _loaded(alice, post)
    edited.body = "an edit"
    edited.folder = edited.folder.sudo(reason="test.folder")
    rows = PermissionAuditEvent.objects.filter(reason="test.folder")
    assert edited.check_save().allowed
    assert rows.count() == 0
    edited.save()
    assert rows.count() == 1


def test_a_swallowed_save_denial_does_not_change_the_direct_verdict(world, monkeypatch):
    _alice, bob, _carol, _mine, _theirs, post = world
    original = Post.save

    def save(self, *args, **kwargs):
        try:
            original(self, *args, **kwargs)
        except Exception:
            pass

    monkeypatch.setattr(Post, "save", save)
    retitled = _loaded(bob, post)
    retitled.title = "a new title"
    assert not retitled.check_save().allowed


def test_no_denial_is_emitted_during_the_check(world, settings, monkeypatch):
    from rebac import audit

    _alice, bob, _carol, _mine, _theirs, post = world
    settings.REBAC_AUDIT_DENIALS = True
    emitted = []
    original = audit.emit
    monkeypatch.setattr(audit, "emit", lambda *a, **k: emitted.append(k) or original(*a, **k))
    retitled = _loaded(bob, post)
    retitled.title = "a new title"
    assert not retitled.check_save().allowed
    assert not [k for k in emitted if str(k.get("reason", "")).startswith("denied:")]
