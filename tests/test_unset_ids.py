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
    relation viewer: auth/user | auth/user:* | auth/group#member
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
# A wildcard subject is a subject, not an unset id.
WILDCARD = RelationshipTuple(ObjectRef("test/doc", "w"), "viewer", SubjectRef.of("auth/user", "*"))


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


def unset_ids(*args, **options):
    out, err = io.StringIO(), io.StringIO()
    call_command("rebac", "unset-ids", *args, stdout=out, stderr=err, **options)
    return out.getvalue().splitlines(), err.getvalue().splitlines()


REMOVE_EXACTLY = (
    "Remove a listed row by its exact stored values with "
    'rebac.relationships.delete_relationship(RelationshipTuple(...)), "" and "None" included. '
    "Never with delete_relationships(RelationshipFilter(...)) and an empty field: an empty "
    'filter field matches every value, so RelationshipFilter(subject_id="") deletes every row '
    "the other fields select."
)


def test_unset_ids_lists_every_kind_of_row_and_nothing_else(storage, monkeypatch):
    store_as_written(monkeypatch, *STORED, NAMED, WILDCARD)
    before = stored()
    listed, notes = unset_ids()
    # Read only: every row is still stored, and the operator is told how to remove one.
    assert stored() == before
    assert notes == [REMOVE_EXACTLY]
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
    write_relationships([NAMED, WILDCARD])
    assert unset_ids("--check") == ([], [])
    store_as_written(monkeypatch, STORED[0])
    with pytest.raises(CommandError, match="1 relationship rows and principals") as raised:
        unset_ids("--check")
    assert raised.value.returncode == 1
    assert len(stored()) == 3


def test_unset_ids_runs_while_a_system_check_error_stops_other_commands(storage, monkeypatch):
    from django.core.management.base import SystemCheckError
    from django.test import override_settings

    store_as_written(monkeypatch, STORED[0])
    with override_settings(REBAC_ALLOW_SUDO="False"):
        with pytest.raises(SystemCheckError, match=r"rebac\.E028"):
            call_command("rebac", "relationships", skip_checks=False, stdout=io.StringIO())
        listed, _ = unset_ids(skip_checks=False)
    assert listed == ["test/doc:d#viewer @ auth/user:\tsubject id is ''"]


def test_unset_ids_says_what_a_storage_it_cannot_read_leaves_unexamined(storage):
    from django.test import override_settings

    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="Registry"):
        listed, notes = unset_ids()
        with pytest.raises(CommandError, match="could not be examined") as raised:
            unset_ids("--check")
    assert listed == []
    assert notes == [
        "Not examined: relationship rows: REBAC_LOCAL_BACKEND_STORAGE is str 'Registry', not "
        '"denormalized" or "registry" (rebac.E006).'
    ]
    assert raised.value.returncode == 3


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
    # Group has no such field: no query can select its rows, and the command says so.
    assert notes == [_not_a_field("auth.Group", "first_name")]


def _not_a_field(label, attr):
    return (
        f"Not examined: {label}: {attr} is not a stored field, so no query can select its rows. "
        f'Examine them in Python: [obj.pk for obj in apps.get_model("{label}")._base_manager'
        f'.iterator() if operator.attrgetter("{attr}")(obj) in (None, "", "None")]'
    )


def test_unset_ids_check_does_not_pass_when_it_could_not_look(monkeypatch, settings):
    """A computed public id (a property) is what a query cannot select."""
    user_model = get_user_model()
    monkeypatch.setattr(
        user_model, "public_id", property(lambda self: self.first_name or None), raising=False
    )
    settings.REBAC_USER_ID_ATTR = "public_id"
    user_model.objects.create(username="svc", first_name="")
    with pytest.raises(CommandError, match="Nothing listed, but 2 could not be examined") as raised:
        unset_ids("--check")
    assert raised.value.returncode == 3
    listed, notes = unset_ids()
    assert listed == []
    assert notes == [
        _not_a_field("auth.Group", "public_id"),
        _not_a_field("auth.User", "public_id"),
    ]


def test_unset_ids_says_what_an_id_attribute_setting_it_cannot_read_leaves_unexamined(settings):
    settings.REBAC_USER_ID_ATTR = None
    with pytest.raises(CommandError, match="could not be examined") as raised:
        unset_ids("--check")
    assert raised.value.returncode == 3
    _, notes = unset_ids()
    assert [note.split(": ")[1] for note in notes] == ["auth.Group", "auth.User"]
    assert all("REBAC_USER_ID_ATTR is NoneType None, not a str" in note for note in notes)


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
    assert notes == [_not_a_field("auth.User", "name")]


def test_unset_ids_lists_nothing_for_saved_principals_by_primary_key():
    get_user_model().objects.create(username="u")
    Group.objects.create(name="g")
    assert unset_ids() == ([], [])


@pytest.mark.parametrize("slug", ["", None])
def test_a_model_owned_principal_without_an_id_is_no_subject(slug, caplog):
    """A model-owned subject (and a user model with a resource type) refuses as the others do.

    ``to_object_ref`` raises ``TypeError`` for an empty resource id; the request resolvers
    take ``NoActorResolvedError`` as "no actor", so the principal is refused with that.
    """
    import logging

    from django.test import RequestFactory

    from rebac.actors import (
        _served_as_anonymous,
        anonymous_actor,
        default_resolver,
        to_subject_ref,
    )
    from rebac.errors import NoActorResolvedError
    from tests.testapp.models import SubjectContainer

    container = SubjectContainer(slug="named", title="t")
    container.pk = 1
    assert str(to_subject_ref(container)) == "blog/subjectcontainer:named#member"
    container.slug = slug
    with pytest.raises(NoActorResolvedError, match="has no slug"):
        to_subject_ref(container)
    container.is_authenticated = True
    request = RequestFactory().get("/")
    request.user = container
    _served_as_anonymous.clear()
    with caplog.at_level(logging.WARNING, logger="rebac"):
        assert default_resolver(request) == anonymous_actor()
    assert [record.getMessage() for record in caplog.records] == [
        "An authenticated testapp.SubjectContainer is served as the anonymous actor: "
        "SubjectContainer instance has no slug; save it before using it as a subject."
    ]


def test_a_model_owned_principal_with_a_missing_id_attribute_is_still_a_type_error():
    """A misconfigured identity attribute is not "no actor": it keeps its own error."""
    from rebac.actors import to_subject_ref
    from tests.testapp.models import SubjectContainer

    container = SubjectContainer(slug="named", title="t")
    container.pk = 1
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SubjectContainer._meta, "rebac_id_attr", "no_such_attribute")
        with pytest.raises(TypeError, match="no_such_attribute"):
            to_subject_ref(container)


def _as_user_model(monkeypatch, model):
    """Resolve ``model`` as the user model, as a user model that declares its own resource type."""
    from rebac import actors

    monkeypatch.setattr(actors, "get_user_model", lambda: model)


@pytest.mark.parametrize("branch", ["model-owned subject", "user model with a resource type"])
@pytest.mark.parametrize("slug", ["", None])
def test_both_resource_branches_refuse_an_unset_id_as_no_actor(monkeypatch, branch, slug):
    from django.test import RequestFactory

    from rebac.actors import anonymous_actor, default_resolver, to_subject_ref
    from rebac.errors import NoActorResolvedError
    from tests.testapp.models import SubjectContainer

    if branch == "user model with a resource type":
        _as_user_model(monkeypatch, SubjectContainer)
    container = SubjectContainer(slug=slug, title="t")
    container.pk = 1
    container.is_authenticated = True
    with pytest.raises(NoActorResolvedError, match="has no slug"):
        to_subject_ref(container)
    request = RequestFactory().get("/")
    request.user = container
    assert default_resolver(request) == anonymous_actor()


@pytest.mark.parametrize("branch", ["model-owned subject", "user model with a resource type"])
@pytest.mark.parametrize(
    ("value", "wire"),
    [
        (0, "0"),
        (False, "False"),
        (
            __import__("uuid").UUID("12345678-1234-5678-1234-567812345678"),
            "12345678-1234-5678-1234-567812345678",
        ),
    ],
    ids=["zero", "false", "uuid"],
)
def test_both_resource_branches_accept_a_falsy_or_typed_id(monkeypatch, branch, value, wire):
    """Only ``None`` and the empty string are unset: ``0`` and ``False`` are ids."""
    from rebac.actors import to_subject_ref
    from tests.testapp.models import SubjectContainer

    if branch == "user model with a resource type":
        _as_user_model(monkeypatch, SubjectContainer)
    container = SubjectContainer(slug="named", title="t")
    container.pk = 1
    container.is_authenticated = True
    container.slug = value
    assert str(to_subject_ref(container)) == f"blog/subjectcontainer:{wire}#member"


def test_unset_ids_says_a_registered_subject_it_cannot_type_is_unexamined(monkeypatch, settings):
    """A type prefix it cannot read leaves a registered model subject unexamined, not a traceback."""
    from rebac import actors
    from tests.testapp.models import SluggedPost

    monkeypatch.setitem(actors._subject_registry, SluggedPost, ("blog/x", "slug"))
    settings.REBAC_TYPE_PREFIX = 5
    with pytest.raises(CommandError, match="could not be examined") as raised:
        unset_ids("--check")
    assert raised.value.returncode == 3
    listed, notes = unset_ids()
    assert listed == []
    assert notes == [
        "Not examined: testapp.SluggedPost: REBAC_TYPE_PREFIX is int 5, not a str or None "
        "(rebac.E028)."
    ]


@pytest.mark.parametrize("check", [False, True])
def test_unset_ids_says_the_relationship_table_is_not_there(storage, monkeypatch, check):
    from django.db import connection

    from rebac.models import RebacResource

    real = connection.introspection.table_names
    gone = {active_relationship_model()._meta.db_table, RebacResource._meta.db_table}
    monkeypatch.setattr(
        connection.introspection,
        "table_names",
        lambda *args, **kwargs: [name for name in real(*args, **kwargs) if name not in gone],
    )
    with pytest.raises(CommandError, match=r"not there: run manage\.py migrate") as raised:
        unset_ids(*(["--check"] if check else []))
    assert raised.value.returncode == 4
    assert active_relationship_model()._meta.db_table in str(raised.value)
