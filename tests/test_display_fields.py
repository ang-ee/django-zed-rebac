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


def test_the_meta_option_is_read_from_the_model():
    class Shown(RebacMixin, models.Model):
        name = models.CharField(max_length=10)

        class Meta:
            abstract = True
            app_label = "testapp"
            rebac_resource_type = "test/shown"
            rebac_display_fields = ("name",)

    assert Shown._meta.rebac_display_fields == ("name",)
