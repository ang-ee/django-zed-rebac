"""``REBAC_DISPLAY_FIELDS`` (proposal 0020): what a referred-to row shows to a reader of the referring row."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, models
from django.test.utils import CaptureQueriesContext

from rebac import PermissionDenied, RebacMixin, display
from rebac.field_visibility import redacted_fields
from rebac.testing import install_schema
from tests.mechanism_policies import ADMIN, FIXED, MEMBER, grant, label_threads
from tests.test_bounded_walk import local as local
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


@pytest.fixture
def threads(local):
    """Two threads labelled by name; the member may read every message and neither thread."""
    backend = install_schema(FIXED)
    label_threads({1: "first", 2: "second"})
    backend.write_relationships(
        [grant("scope/message", n, "direct") for n in (1, 2, 3)]
        + [grant("scope/role", "admin", "member", ADMIN)]
    )
    display.declare(ScopeThread, "label")
    yield backend
    display.declare(ScopeThread)


def messages(actor=MEMBER, *paths):
    with CaptureQueriesContext(connection) as captured:
        rows = list(
            ScopeMessage.objects.with_actor(actor)
            .rebac_select_related(*(paths or ("thread",)))
            .order_by("pk")
        )
    return rows, [query["sql"] for query in captured.captured_queries]


def test_off_a_related_row_the_actor_may_not_read_is_an_error(threads):
    with pytest.raises(PermissionDenied, match="outside actor scope"):
        messages()


def test_fallback_shows_display_fields_where_the_actor_may_not_read_the_row(threads, settings):
    settings.REBAC_DISPLAY_FIELDS = "fallback"
    rows, _statements = messages()
    assert [row.thread.label for row in rows] == ["first", "first", "second"]
    assert all(display.is_display_row(row.thread) for row in rows)
    assert all(row.thread.pk == row.thread_id for row in rows)
    # A row the actor holds is delivered whole, as before.
    threads.write_relationships([grant("scope/thread", 2, "reader")])
    rows, _statements = messages()
    assert [display.is_display_row(row.thread) for row in rows] == [True, True, False]
    assert redacted_fields(rows[2].thread) == frozenset()


def test_always_issues_no_permission_statement_for_the_relation(threads, settings):
    _rows, checked = messages(ADMIN)
    settings.REBAC_DISPLAY_FIELDS = True
    rows, statements = messages(ADMIN)
    # Even a row the actor holds is a display row: the relation is not asked about.
    assert all(display.is_display_row(row.thread) for row in rows)
    assert [row.thread.label for row in rows] == ["first", "first", "second"]
    assert len(statements) < len(checked)
    assert not any("testapp_scopethread" in sql and "rebac" in sql for sql in statements[1:])


def test_a_display_row_hides_every_other_field(local, settings):
    """A part's message is a display row: its foreign key to the thread is hidden, the thread
    below it is cut off, and it is not checked."""
    backend = install_schema(FIXED)
    backend.write_relationships([grant("scope/thread", 1, "reader")])
    display.declare(ScopeMessage, "id")
    try:
        settings.REBAC_DISPLAY_FIELDS = True
        parts = list(
            ScopePart.objects.with_actor(MEMBER)
            .rebac_select_related("message__thread")
            .order_by("pk")
        )
        assert [part.pk for part in parts] == [1, 2]
        message = parts[0].message
        assert display.is_display_row(message) and message.pk == 1
        assert redacted_fields(message) == {"thread"}
        # A hidden foreign key reads as absent, as a field its gate denies does.
        assert message.thread_id is None
        with pytest.raises(ScopeMessage.thread.RelatedObjectDoesNotExist):
            _ = message.thread
    finally:
        display.declare(ScopeMessage)


def test_a_model_without_display_fields_behaves_as_before(local, settings):
    backend = install_schema(FIXED)
    backend.write_relationships([grant("scope/message", 1, "direct")])
    for setting in ("fallback", True):
        settings.REBAC_DISPLAY_FIELDS = setting
        with pytest.raises(PermissionDenied, match="outside actor scope"):
            messages()
    backend.write_relationships([grant("scope/thread", 1, "reader")])
    rows, _statements = messages()
    assert [row.pk for row in rows] == [1, 2] and not display.is_display_row(rows[0].thread)


def test_a_declaration_must_name_the_models_own_columns():
    display.declare(ScopeThread, "missing")
    try:
        with pytest.raises(ImproperlyConfigured, match="not a field"):
            display.fields(ScopeThread)
    finally:
        display.declare(ScopeThread)
    assert display.fields(ScopeThread) == frozenset()


def test_a_relation_is_refused_as_a_display_field(local, settings):
    """Shown, a foreign key let a joined display row load the row it names, whole and unchecked,
    by following it. A display field is a plain column; the related row has display fields of
    its own."""
    backend = install_schema(FIXED)
    backend.write_relationships([grant("scope/message", 1, "direct")])
    settings.REBAC_DISPLAY_FIELDS = True
    display.declare(ScopeMessage, "id", "thread")
    try:
        with pytest.raises(ImproperlyConfigured, match="is a relation"):
            list(ScopePart.objects.with_actor(MEMBER).rebac_select_related("message"))
    finally:
        display.declare(ScopeMessage)


def test_a_one_to_one_is_refused_as_a_display_field():
    from tests.testapp.models import ScopeProfile

    display.declare(ScopeProfile, "nick", "owner")
    try:
        with pytest.raises(ImproperlyConfigured, match="'owner' is a relation"):
            display.fields(ScopeProfile)
    finally:
        display.declare(ScopeProfile)


def test_a_model_whose_primary_key_is_a_relation_declares_no_display_fields():
    """A display row shows its key. A card's key is a one-to-one to an account that is not a
    parent link: following it from a display card read the account whole, unchecked. Such a
    model is refused wherever its declaration is read, and so is a multi-table child of it, whose
    own key is a parent link but whose parent's key is that one-to-one; a multi-table child's
    parent link is not such a key."""
    from tests.testapp.models import LabCard, LabGiftCard, LabShop

    display.declare(LabCard, "label")
    display.declare(LabGiftCard, "amount")
    display.declare(LabShop, "till")
    try:
        with pytest.raises(ImproperlyConfigured, match="primary key 'account' of LabCard"):
            display.fields(LabCard)
        with pytest.raises(
            ImproperlyConfigured, match="LabGiftCard: the primary key 'account' of LabCard"
        ):
            display.fields(LabGiftCard)
        assert display.declaration_problems(LabShop) == []
        assert e026() == [
            ("testapp.LabCard", "primary key"),
            ("testapp.LabGiftCard", "primary key"),
        ]
    finally:
        display.declare(LabCard)
        display.declare(LabGiftCard)
        display.declare(LabShop)


@pytest.mark.parametrize("setting", [False, "fallback", True])
def test_the_system_check_does_not_depend_on_the_setting(settings, setting):
    """A wrong declaration is a hole the moment the setting is turned on: it is reported with the
    setting off too."""
    settings.REBAC_DISPLAY_FIELDS = setting
    display.declare(ScopeMessage, "id", "thread")
    try:
        assert e026() == [("testapp.ScopeMessage", "is a relation")]
    finally:
        display.declare(ScopeMessage)


def e026() -> list[tuple[str, str]]:
    """``(model label, what is wrong)`` of every ``rebac.E026`` the display check reports."""
    from rebac.checks import check_display_declarations

    kinds = ("RebacMixin", "not a field", "own columns", "primary key", "is a relation")
    return sorted(
        (str(message.obj._meta.label), next(kind for kind in kinds if kind in message.msg))
        for message in check_display_declarations()
        if message.id == "rebac.E026"
    )


def test_the_system_check_reports_every_wrong_declaration_at_startup(monkeypatch):
    """Both ways of declaring, every condition the first read would refuse."""
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.core.management.base import SystemCheckError

    from tests.testapp.models import ScopeProfile

    assert e026() == []
    user = get_user_model()
    display.declare(ScopeMessage, "id", "thread", "profile", "missing")
    display.declare(ScopeProfile, "nick", "owner")
    display.declare(user, "username")
    monkeypatch.setattr(
        ScopeThread._meta, "rebac_display_fields", ("label", "scopemessage"), raising=False
    )
    try:
        assert e026() == sorted(
            [
                ("testapp.ScopeMessage", "is a relation"),
                ("testapp.ScopeMessage", "own columns"),
                ("testapp.ScopeMessage", "not a field"),
                ("testapp.ScopeProfile", "is a relation"),
                ("testapp.ScopeThread", "own columns"),
                (user._meta.label, "RebacMixin"),
            ]
        )
        with pytest.raises(SystemCheckError, match=r"rebac\.E026"):
            call_command("check")
    finally:
        for model in (ScopeMessage, ScopeProfile, user):
            display.declare(model)
    # The declare() calls are withdrawn; the Meta declaration is still the model's.
    assert e026() == [("testapp.ScopeThread", "own columns")]


def test_the_meta_option_is_read_from_the_model():
    class Shown(RebacMixin, models.Model):
        name = models.CharField(max_length=10)

        class Meta:
            abstract = True
            app_label = "testapp"
            rebac_resource_type = "test/shown"
            rebac_display_fields = ("name",)

    assert Shown._meta.rebac_display_fields == ("name",)


JOINED = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = direct
}
definition scope/profile {
    relation owner: scope/message // rebac:field=owner
    relation reader: auth/user
    permission read = reader
    permission write = reader
}
"""


@pytest.fixture
def profiled(local):
    """Message 1 has a profile and sits in thread 1. The member reads the message alone."""
    from tests.testapp.models import ScopeProfile

    backend = install_schema(JOINED)
    ScopeProfile._base_manager.bulk_create([ScopeProfile(id=1, owner_id=1, nick="nick")])
    backend.write_relationships([grant("scope/message", 1, "direct")])
    display.declare(ScopeProfile, "nick")
    yield backend
    display.declare(ScopeProfile)


def joined():
    return list(
        ScopeMessage.objects.with_actor(MEMBER)
        .filter(pk=1)
        .rebac_select_related("profile", "thread")
    )


@pytest.mark.parametrize("setting", ["fallback", True])
def test_a_display_row_does_not_hide_its_siblings_from_the_guard(profiled, settings, setting):
    """Django caches the message back on the profile it joined. The profile's hidden key to the
    message must not take the message's other relations out of the check with it."""
    with pytest.raises(PermissionDenied, match="outside actor scope"):
        joined()
    settings.REBAC_DISPLAY_FIELDS = setting
    with pytest.raises(PermissionDenied, match="ScopeThread"):
        joined()
    profiled.write_relationships([grant("scope/thread", 1, "reader")])
    [message] = joined()
    assert message.thread.pk == 1 and not display.is_display_row(message.thread)
    # The display row is still the message's profile, and shows what it declares.
    assert display.is_display_row(message.profile) and message.profile.nick == "nick"
    assert message.profile.owner_id is None and "owner" in redacted_fields(message.profile)


def test_a_field_gate_added_by_a_later_policy_is_honoured(profiled, settings):
    from tests.testapp.models import ScopeProfile

    assert display.fields(ScopeProfile) == {"nick"}
    gated = JOINED.replace(
        "    permission write = reader\n}",
        "    permission write = reader\n    permission read__nick = reader\n}",
    )
    assert gated != JOINED
    install_schema(gated)
    # The gate decides the field now; the declaration no longer shows it.
    assert display.fields(ScopeProfile) == frozenset()


def test_only_a_model_whose_save_leaves_hidden_fields_alone_declares_display_fields():
    from django.contrib.auth import get_user_model

    user = get_user_model()
    display.declare(user, "username")
    try:
        with pytest.raises(ImproperlyConfigured, match="RebacMixin"):
            display.fields(user)
    finally:
        display.declare(user)


def test_saving_a_display_row_writes_no_hidden_column(profiled, settings):
    from tests.testapp.models import ScopeProfile

    profiled.write_relationships(
        [grant("scope/thread", 1, "reader"), grant("scope/profile", 1, "reader")]
    )
    settings.REBAC_DISPLAY_FIELDS = True
    [message] = joined()
    profile = message.profile
    assert display.is_display_row(profile) and profile.owner_id is None
    profile.nick = "renamed"
    profile.save()
    stored = ScopeProfile._base_manager.get(pk=1)
    assert stored.nick == "renamed" and stored.owner_id == 1
