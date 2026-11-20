"""Rows and principals whose id is unset: found by ``rebac unset-ids``, read and removed as stored.

0.37 and earlier stored a subject resolved from an instance whose id attribute was unset
under the id ``"None"`` (``str(None)``), and accepted an empty subject id; earlier still,
an empty resource id. A direct write to the relationship models stores any of them now.
The library no longer writes them, and every one stays readable and removable through
its own API.
"""

from __future__ import annotations

import io

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError

from rebac import backend, memberships, sudo
from rebac.backends import reset_backend
from rebac.backends.local import LocalBackend
from rebac.models import active_relationship_model
from rebac.relationships import delete_relationship, write_relationships
from rebac.schema import parse_zed
from rebac.types import ObjectRef, PermissionResult, RelationshipTuple, SubjectRef
from tests.backend_setup import install_schema

# On PostgreSQL the listing follows another collation of the ids than on SQLite.
pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]

SCHEMA = """
definition auth/user {}

definition auth/group {
    relation member: auth/user
}

definition test/role {
    relation member: auth/user | auth/group#member
}

definition test/doc {
    relation viewer: auth/user | auth/group#member
    permission view = viewer
}
"""

ALICE = SubjectRef.of("auth/user", "alice")
DOC = ObjectRef("test/doc", "d")
ROLE = ObjectRef("test/role", "r")

# One row of each kind an earlier version, or a direct write, can have stored.
STORED = (
    RelationshipTuple(DOC, "viewer", SubjectRef.of("auth/user", "")),
    RelationshipTuple(DOC, "viewer", SubjectRef.of("auth/user", "None")),
    RelationshipTuple(ROLE, "member", SubjectRef.of("auth/group", "", "member")),
    RelationshipTuple(ROLE, "member", SubjectRef.of("auth/group", "None", "member")),
    RelationshipTuple(ObjectRef("test/doc", ""), "viewer", ALICE),
    RelationshipTuple(ObjectRef("test/doc", "None"), "viewer", ALICE),
)
# A row that names objects; the finder never lists it.
NAMED = RelationshipTuple(DOC, "viewer", ALICE)


@pytest.fixture(params=["denormalized", "registry"])
def storage(request, settings):
    """Rows: in both storage layouts."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA))
    yield request.param
    reset_backend()


def store_as_written(monkeypatch, *tuples):
    """Store rows past the id validation, as an earlier version or a direct write did."""
    with monkeypatch.context() as patch:
        patch.setattr(LocalBackend, "_validate_relationship_tuple", lambda *a, **k: None)
        backend().write_relationships(list(tuples))


def stored():
    return sorted(str(row) for row in active_relationship_model()._base_manager.all())


def unset_ids(*args):
    out, err = io.StringIO(), io.StringIO()
    call_command("rebac", "unset-ids", *args, stdout=out, stderr=err)
    return out.getvalue().splitlines(), err.getvalue().splitlines()


def test_unset_ids_lists_every_kind_of_row_and_nothing_else(storage, monkeypatch):
    store_as_written(monkeypatch, *STORED, NAMED)
    listed, _ = unset_ids()
    # In the listing's order, which is the database's collation of the ids.
    assert sorted(listed) == [
        "test/doc:#viewer @ auth/user:alice\tresource id is ''",
        "test/doc:None#viewer @ auth/user:alice\tresource id is 'None'",
        "test/doc:d#viewer @ auth/user:\tsubject id is ''",
        "test/doc:d#viewer @ auth/user:None\tsubject id is 'None'",
        "test/role:r#member @ auth/group:#member\tsubject id is ''",
        "test/role:r#member @ auth/group:None#member\tsubject id is 'None'",
    ]


def test_unset_ids_names_both_sides_of_one_row(storage, monkeypatch):
    store_as_written(
        monkeypatch,
        RelationshipTuple(ObjectRef("test/doc", "None"), "viewer", SubjectRef.of("auth/user", "")),
    )
    assert unset_ids()[0] == [
        "test/doc:None#viewer @ auth/user:\tresource id is 'None', subject id is ''"
    ]


def test_unset_ids_check_fails_only_when_something_is_listed(storage, monkeypatch):
    write_relationships([NAMED])
    assert unset_ids("--check") == ([], [])
    store_as_written(monkeypatch, STORED[0])
    with pytest.raises(CommandError, match="1 relationship rows and principals"):
        unset_ids("--check")


@pytest.mark.parametrize("tuple_", STORED, ids=str)
def test_every_stored_kind_is_removable_by_its_exact_tuple(storage, monkeypatch, tuple_):
    store_as_written(monkeypatch, *STORED)
    delete_relationship(tuple_)
    assert str(tuple_) not in stored()
    assert len(stored()) == len(STORED) - 1
    assert len(unset_ids()[0]) == len(STORED) - 1


def test_stored_member_rows_are_revoked_through_memberships_and_the_command(storage, monkeypatch):
    store_as_written(monkeypatch, *STORED[2:4])
    assert (
        memberships.revoke(subject=SubjectRef.of("auth/group", "", "member"), container=ROLE) == 1
    )
    out = io.StringIO()
    call_command("rebac", "revoke", "test/role:r", "auth/group:None#member", stdout=out)
    assert out.getvalue().strip() == "1"
    assert stored() == []


def test_stored_rows_are_listed_by_subject(storage, monkeypatch):
    store_as_written(monkeypatch, *STORED)
    model = active_relationship_model()
    assert [str(row) for row in model.objects.for_subject("auth/user", "")] == [
        "test/doc:d#viewer @ auth/user:"
    ]
    out = io.StringIO()
    call_command("rebac", "relationships", "--subject", "auth/user:None", stdout=out)
    assert out.getvalue().splitlines() == ["test/doc:d#viewer @ auth/user:None"]


def test_a_none_id_names_the_empty_row_and_the_text_none_names_its_own(storage, monkeypatch):
    """``SubjectRef.of(type, None)`` is the empty id: it holds the row stored with ``""``.

    The row stored under ``"None"`` is named only by that text, and a ``None`` subject id
    removes the empty row, not that one.
    """
    store_as_written(
        monkeypatch,
        RelationshipTuple(DOC, "viewer", SubjectRef.of("auth/user", "")),
        RelationshipTuple(ObjectRef("test/doc", "e"), "viewer", SubjectRef.of("auth/user", "None")),
    )

    def view(subject, resource):
        return backend().check_access(subject=subject, action="view", resource=resource).result

    of_none = SubjectRef.of("auth/user", None)
    assert view(of_none, DOC) == PermissionResult.HAS_PERMISSION
    assert view(of_none, ObjectRef("test/doc", "e")) == PermissionResult.NO_PERMISSION
    assert view(SubjectRef.of("auth/user", "None"), ObjectRef("test/doc", "e")) == (
        PermissionResult.HAS_PERMISSION
    )
    delete_relationship(RelationshipTuple(DOC, "viewer", of_none))
    assert stored() == ["test/doc:e#viewer @ auth/user:None"]


def test_writing_a_stored_empty_subject_again_refuses_the_whole_batch(storage, monkeypatch):
    store_as_written(monkeypatch, STORED[0])
    with pytest.raises(ValueError, match="Subject IDs cannot be empty"):
        write_relationships([NAMED, STORED[0]])
    assert stored() == ["test/doc:d#viewer @ auth/user:"]


def test_unset_ids_lists_users_and_groups_by_their_id_attribute(settings):
    settings.REBAC_USER_ID_ATTR = "first_name"
    users = get_user_model().objects
    empty = users.create(username="empty", first_name="")
    text = users.create(username="text", first_name="None")
    users.create(username="named", first_name="Ann")
    listed, notes = unset_ids()
    assert listed == [
        f"auth.User pk={empty.pk!r}\tfirst_name is ''",
        f"auth.User pk={text.pk!r}\tfirst_name is 'None'",
    ]
    # Group has no such field: the attribute cannot be read for it, and the command says so.
    assert notes == ["auth.Group: first_name is not a stored field; its rows are not read"]


def test_unset_ids_lists_a_null_id_attribute(settings):
    settings.REBAC_USER_ID_ATTR = "last_login"
    never = get_user_model().objects.create(username="never")
    listed, _ = unset_ids()
    assert listed == [f"auth.User pk={never.pk!r}\tlast_login is None"]


def test_unset_ids_lists_groups_and_registered_subjects(monkeypatch, settings):
    from rebac import actors
    from tests.testapp.models import Folder, PinnedPost, PostPin

    settings.REBAC_USER_ID_ATTR = "name"  # Group.name; the user model has no such field
    group = Group.objects.create(name="None")
    Group.objects.create(name="staff")
    monkeypatch.setitem(actors._subject_registry, PostPin, ("test/pin", "note"))
    with sudo(reason="fixture rows"):
        folder = Folder._base_manager.create(name="f")
        post = PinnedPost._base_manager.create(title="p")
    pin = PostPin.objects.create(post=post, folder=folder, note="")
    PostPin.objects.create(post=post, folder=folder, note="kept")
    listed, notes = unset_ids()
    assert listed == [
        f"auth.Group pk={group.pk!r}\tname is 'None'",
        f"testapp.PostPin pk={pin.pk!r}\tnote is ''",
    ]
    assert notes == ["auth.User: name is not a stored field; its rows are not read"]


def test_unset_ids_lists_nothing_for_saved_principals_by_primary_key():
    get_user_model().objects.create(username="u")
    Group.objects.create(name="g")
    assert unset_ids() == ([], [])
