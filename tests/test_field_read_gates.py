"""Field-level read enforcement.

``read__<field>`` is a normal permission name in the schema. These tests pin
the model-layer redaction behavior that consumes those permissions for every
transport, not just GraphQL.
"""

from __future__ import annotations

import pickle
from typing import Any, cast

import pytest
from django.db import connection
from django.db.models import Count, F, FilteredRelation, Min, Value
from django.db.models.expressions import RawSQL
from django.db.models.functions import Upper
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import (
    ObjectRef,
    PermissionDenied,
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
    relation editor: auth/user
    relation viewer: auth/user

    permission read = owner + editor + viewer
    permission write = owner + editor
    permission read__title = owner
}
"""


NO_READ_GATE_SCHEMA_TEXT = """
definition auth/user {}

definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user

    permission read = owner + viewer
    permission write = owner
}
"""


CAVEAT_SCHEMA_TEXT = """
caveat link_not_expired(expires_at timestamp, now timestamp) {
    now < expires_at
}

definition auth/user {}

definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user
    relation gated_reader: auth/user with link_not_expired

    permission read = owner + viewer + gated_reader
    permission write = owner
    permission read__title = gated_reader
}
"""


WRITE_GATE_WITH_REDACTED_BODY_SCHEMA_TEXT = """
definition auth/user {}

definition blog/folder {
    relation owner: auth/user
    permission read = owner
    permission write = owner
}

definition blog/post {
    relation owner: auth/user
    relation editor: auth/user
    relation viewer: auth/user

    permission read = owner + editor + viewer
    permission write = owner + editor
    permission write__title = owner
    permission read__body = owner
}
"""


FOLDER_GATE_SCHEMA_TEXT = """
definition auth/user {}

definition blog/folder {
    relation owner: auth/user
    relation viewer: auth/user
    permission read = owner + viewer
    permission write = owner
    permission read__name = owner
}

definition blog/post {
    relation owner: auth/user
    relation editor: auth/user
    relation viewer: auth/user

    permission read = owner + editor + viewer
    permission write = owner + editor
    permission read__title = owner
}
"""


SLUGGED_SCHEMA_TEXT = """
definition auth/user {}

definition blog/sluggedpost {
    relation owner: auth/user
    relation editor: auth/user
    relation viewer: auth/user

    permission read = owner + editor + viewer
    permission write = owner + editor
    permission read__slug = owner
}
"""


PAST = "1999-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"


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
def bob(db):
    from django.contrib.auth import get_user_model

    return atomic_source_write(get_user_model().objects.create, username="bob", is_active=True)


def _post(*, title: str, body: str = ""):
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        return Post.objects.create(title=title, body=body)


def _folder(*, name: str):
    from tests.testapp.models import Folder

    with sudo(reason="test.fixture"):
        return Folder.objects.create(name=name)


def _slugged_post(*, slug: str, title: str):
    from tests.testapp.models import SluggedPost

    with sudo(reason="test.fixture"):
        return SluggedPost.objects.create(slug=slug, title=title)


def _grant(
    post_pk: int,
    user: Any,
    relation: str,
    *,
    caveat_name: str = "",
    caveat_context: dict[str, Any] | None = None,
) -> None:
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", str(post_pk)),
                relation=relation,
                subject=SubjectRef.of("auth/user", str(user.pk)),
                caveat_name=caveat_name,
                caveat_context=caveat_context or {},
            ),
        ]
    )


def _grant_ref(resource_type: str, resource_id: str, user: Any, relation: str) -> None:
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef(resource_type, resource_id),
                relation=relation,
                subject=SubjectRef.of("auth/user", str(user.pk)),
            ),
        ]
    )


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_redaction_is_per_row_not_a_blanket_defer(alice, bob):
    from tests.testapp.models import Post

    alice_post = _post(title="alice title")
    bob_post = _post(title="bob title")
    _grant(alice_post.pk, alice, "owner")
    _grant(alice_post.pk, bob, "viewer")
    _grant(bob_post.pk, bob, "owner")
    _grant(bob_post.pk, alice, "viewer")

    rows = list(Post.objects.as_user(alice).order_by("pk"))

    assert [row.pk for row in rows] == [alice_post.pk, bob_post.pk]
    assert rows[0].title == "alice title"
    assert getattr(rows[0], "_rebac_redacted_fields", frozenset()) == frozenset()
    assert rows[1].title is None
    assert rows[1]._rebac_redacted_fields == frozenset({"title"})


def test_on_field_deny_omit_overrides_global_allow_and_survives_clones(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="not for alice")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    row = Post.objects.as_user(alice).on_field_deny("omit").filter(pk=post.pk).get()

    assert row.title is None
    assert row._rebac_redacted_fields == frozenset({"title"})
    assert row._rebac_omitted_fields == frozenset({"title"})


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_for_write_keeps_row_scope_but_disables_field_redaction(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="secret title")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "editor")  # can write, but read__title=owner redacts on a normal read

    # A normal read redacts the gated field for the non-owner editor.
    redacted = Post.objects.as_user(alice).get(pk=post.pk)
    assert redacted.title is None
    assert redacted._rebac_redacted_fields == frozenset({"title"})

    # for_write() = on_field_deny("allow"): redaction off so the write target carries
    # every column, while row scope still applies.
    writable = Post.objects.as_user(alice).for_write().get(pk=post.pk)
    assert writable.title == "secret title"
    assert getattr(writable, "_rebac_redacted_fields", frozenset()) == frozenset()

    # Row scope is preserved: a row the actor cannot access is still not returned.
    unrelated = _post(title="bob only")
    _grant(unrelated.pk, bob, "owner")
    assert Post.objects.as_user(alice).for_write().filter(pk=unrelated.pk).first() is None


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_values_projection_of_gated_field_fails_closed(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="projected secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    with pytest.raises(PermissionDenied) as excinfo:
        list(Post.objects.as_user(alice).values("title"))
    assert "read__" in str(excinfo.value)
    assert "title" in str(excinfo.value)


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_values_list_projection_of_gated_field_fails_closed(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="projected secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    with pytest.raises(PermissionDenied):
        list(Post.objects.as_user(alice).values_list("title", flat=True))


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_values_without_field_list_fails_closed_when_model_has_read_gates(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="projected secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    with pytest.raises(PermissionDenied):
        list(Post.objects.as_user(alice).values())


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_iterator_materialises_model_instances_with_field_redaction(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="streamed secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    row = next(Post.objects.as_user(alice).filter(pk=post.pk).iterator())

    assert row.title is None
    assert row._rebac_redacted_fields == frozenset({"title"})


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_values_iterator_of_gated_field_fails_closed(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="streamed projection secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    with pytest.raises(PermissionDenied):
        next(Post.objects.as_user(alice).values_list("title", flat=True).iterator())


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_pk_values_projection_remains_allowed_with_read_gates(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="pk projection")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    assert list(Post.objects.as_user(alice).values_list("pk", flat=True)) == [post.pk]


@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("project", [False, True])
def test_annotation_cannot_copy_a_gated_field(alice, project):
    from tests.testapp.models import Post

    post = _post(title="aliased secret")
    _grant(post.pk, alice, "viewer")
    qs = Post.objects.as_user(alice).annotate(copied_title=F("title"))
    if project:
        qs = qs.values("id", "copied_title")

    with pytest.raises(PermissionDenied):
        list(qs)


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_projection_of_only_computed_values_names_no_gated_field(alice):
    from tests.testapp.models import Post

    post = _post(title="computed only")
    _grant(post.pk, alice, "viewer")
    rows = Post.objects.as_user(alice).annotate(marker=Upper(Value("x")))

    # The projection names no model field, and the expression reads none.
    assert list(rows.values_list("marker", flat=True)) == ["X"]
    assert list(rows.values("marker")) == [{"marker": "X"}]


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_projection_of_only_a_computed_value_that_reads_a_gated_field_fails_closed(alice):
    from tests.testapp.models import Post

    post = _post(title="computed secret")
    _grant(post.pk, alice, "viewer")
    rows = Post.objects.as_user(alice).annotate(shouted=Upper("title"))

    with pytest.raises(PermissionDenied) as excinfo:
        list(rows.values_list("shouted", flat=True))
    assert "read__title" in str(excinfo.value)


@pytest.fixture
def folder_gates(alice, bob):
    """A post alice may read in a folder alice may read; ``title`` and ``name`` are gated."""
    from tests.testapp.models import Folder, Post

    reset_backend()
    install_schema(backend(), parse_zed(FOLDER_GATE_SCHEMA_TEXT))
    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="folder secret")
        post = Post.objects.create(title="post secret", folder=folder)
    _grant_ref("blog/folder", str(folder.pk), bob, "owner")
    _grant_ref("blog/folder", str(folder.pk), alice, "viewer")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    return folder, post


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_combination_of_instances_loads_every_field_of_one_model(alice, folder_gates):
    from tests.testapp.models import Folder, Post

    _folder, post = folder_gates
    rows = Post.objects.as_user(alice)
    folders = Folder.objects.as_user(alice)
    related = rows.rebac_select_related("folder")
    # Rows of an operand become instances of the first operand's model column
    # by column: ``title`` would arrive as ``body``, which nothing redacts.
    for combined in (
        lambda: rows.only("id", "body").union(rows.only("id", "title")),
        lambda: rows.defer("title").union(rows.defer("body")),
        lambda: rows.only("id", "body").union(folders.only("id", "name")),
        lambda: rows.only("id", "body").union(
            rows.only("id", "body").union(rows.only("id", "title"))
        ),
        lambda: rows.only("id", "body", "folder").annotate(extra=Value("x")).union(rows.only()),
        lambda: rows.only("id", "body", "folder").union(rows.defer("folder__kind")),
        lambda: related.only("id", "folder", "folder__kind").union(
            related.only("id", "folder", "folder__name")
        ),
        lambda: rows.select_related("folder").union(rows.select_related("folder")),
        # No layout is worked out: the same columns named differently are refused too.
        lambda: rows.only("id", "body").union(rows.only("id", "body")),
    ):
        with pytest.raises(PermissionDenied) as excinfo:
            list(combined())
        assert "must load every field of one model" in str(excinfo.value)
    # Whole rows of one model are redacted as any of its rows are.
    for combined in (rows.union(rows), rows.union(rows, all=True), rows.intersection(rows)):
        assert {(row.pk, row.title) for row in combined} == {(post.pk, None)}


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_gated_column_of_a_joined_model_is_guarded(alice, folder_gates):
    from tests.testapp.models import Folder, Post

    folder, _post_row = folder_gates
    rows = Post.objects.as_user(alice)
    for attempt in (
        lambda: list(rows.values_list("folder__name", flat=True)),
        lambda: list(rows.annotate(n=F("folder__name")).values_list("n", flat=True)),
        lambda: [row.n for row in rows.annotate(n=F("folder__name"))],
        lambda: list(rows.annotate(f=FilteredRelation("folder")).values_list("f__name")),
        lambda: list(Folder.objects.as_user(alice).values_list("parent__name", flat=True)),
        lambda: list(
            rows.values_list("body").union(Folder.objects.as_user(alice).values_list("name"))
        ),
    ):
        with pytest.raises(PermissionDenied) as excinfo:
            attempt()
        assert "read__name on Folder" in str(excinfo.value)
    # A column of the joined model that has no gate is read as before.
    assert list(rows.values_list("folder__kind", "folder_id")) == [("", folder.pk)]
    assert [row.kind for row in rows.annotate(kind=F("folder__kind"))] == [""]


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_an_annotation_an_operand_does_not_select_is_not_refused(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="hidden annotation")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    rows = Post.objects.as_user(alice).annotate(copied=F("title"))
    # The combination selects ``pk`` alone, in each operand.
    assert list(rows.union(rows).values_list("pk", flat=True)) == [post.pk]
    with pytest.raises(PermissionDenied):
        list(rows.union(rows).values_list("copied", flat=True))


INHERITED_GATE_SCHEMA_TEXT = """
definition auth/user {}

definition test/nativeparentlinkedresource {}

definition test/nativeparentlinkedchild {
    relation owner: auth/user
    relation viewer: auth/user
    permission read = owner + viewer
    permission read__name = owner
}

definition test/nativeparentlinkedrecord {
    relation viewer: auth/user
    permission read = viewer
}
"""


@pytest.mark.xfail(
    strict=True,
    reason=(
        "proposal 0013: a column read through a join is gated by the model that owns the "
        "column; a field a multi-table child inherits belongs to its parent's table, and "
        "the guard does not know which model the join came through"
    ),
)
@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_gated_field_a_joined_child_inherits_is_guarded(alice, bob):
    from tests.testapp.models import NativeParentLinkedChild, NativeParentLinkedRecord

    reset_backend()
    install_schema(backend(), parse_zed(INHERITED_GATE_SCHEMA_TEXT))
    with sudo(reason="test.fixture"):
        child = NativeParentLinkedChild.objects.create(name="inherited secret", owner=bob)
        record = NativeParentLinkedRecord.objects.create(child=child)
    _grant_ref("test/nativeparentlinkedchild", str(child.pk), bob, "owner")
    _grant_ref("test/nativeparentlinkedchild", str(child.pk), alice, "viewer")
    _grant_ref("test/nativeparentlinkedrecord", str(record.pk), alice, "viewer")
    rows = NativeParentLinkedRecord.objects.as_user(alice)
    with pytest.raises(PermissionDenied):
        list(rows.values_list("child__name", flat=True))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "proposal 0013: Django compiles the operands of a combination itself, so a root "
        "that is not a rebac queryset never applies the scope of a rebac operand"
    ),
)
@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_plain_root_keeps_the_scope_of_a_rebac_operand(alice, bob):
    from tests.testapp.models import BackingStage, Post

    post = _post(title="scoped title")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _post(title="not alice's row")
    scoped = Post.objects.as_user(alice).values_list("body")
    with sudo(reason="test.fixture"):
        BackingStage.objects.create(hidden=None)
    combined = list(BackingStage.objects.values_list("hidden").union(scoped, all=True))
    # One stage and the one post alice may read.
    assert len(combined) == 2


@pytest.mark.parametrize("schema", [SCHEMA_TEXT, NO_READ_GATE_SCHEMA_TEXT], ids=["gated", "plain"])
def test_hand_written_sql_is_selected_under_sudo_only(alice, bob, schema):
    from tests.testapp.models import Post

    reset_backend()
    install_schema(backend(), parse_zed(schema))
    post = _post(title="literal secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    column = f'"{Post._meta.db_table}"."title"'
    rows = Post.objects.as_user(alice)
    attempts = (
        lambda: list(rows.annotate(t=RawSQL(column, [])).values_list("t", flat=True)),
        lambda: list(rows.extra(select={"t": column}).values_list("t", flat=True)),
        lambda: [row.t for row in rows.annotate(t=RawSQL(column, []))],
        lambda: [row.t for row in rows.extra(select={"t": column})],
        lambda: list(rows.annotate(t=Upper(RawSQL(column, []))).values("t")),
        lambda: list(
            rows.values_list("pk").union(rows.extra(select={"t": column}).values_list("t"))
        ),
    )
    with override_settings(REBAC_FIELD_READ_MODE="redact"):
        # What hand-written SQL reads cannot be told from the expression,
        # whichever model it is selected on.
        for attempt in attempts:
            with pytest.raises(PermissionDenied) as excinfo:
                attempt()
            assert "hand-written SQL" in str(excinfo.value)
        # SQL that is defined but not selected reads nothing.
        assert list(rows.annotate(t=RawSQL(column, [])).values_list("pk", flat=True)) == [post.pk]
        with sudo(reason="test.literal"):
            assert [row.t for row in Post.objects.annotate(t=RawSQL(column, []))] == [
                "literal secret"
            ]
    # Without field read enforcement there is nothing to check it against.
    assert attempts[0]() == ["literal secret"]


@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("combine", ["union", "union_all", "intersection", "difference"])
def test_every_operand_of_a_set_combination_is_guarded(alice, bob, combine):
    from tests.testapp.models import Post

    post = _post(title="operand secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    rows = Post.objects.as_user(alice)

    def combined(left, right):
        if combine == "union_all":
            return left.union(right, all=True)
        return getattr(left, combine)(right)

    public = rows.annotate(marker=Value("public")).values("marker")
    copied = rows.annotate(marker=F("title")).values("marker")
    # A combination returns the columns of each operand, not of the first.
    for left, right in ((public, copied), (copied, public)):
        with pytest.raises(PermissionDenied) as excinfo:
            list(combined(left, right))
        assert "read__title" in str(excinfo.value)
    for left, right in (("body", "title"), ("title", "body")):
        with pytest.raises(PermissionDenied):
            list(combined(rows.values_list(left), rows.values_list(right)))
    with pytest.raises(PermissionDenied):
        list(combined(public, combined(public, copied)))
    # Operands with projections of their own keep them, whatever the
    # combination is asked for afterwards: Django runs the operands.
    renamed = combined(rows.values("body"), rows.values("body")).values("title")
    assert {row["title"] for row in renamed} == (set() if combine == "difference" else {""})
    # Operands that read no gated field combine as before.
    other = rows.annotate(marker=Value("other")).values("marker")
    assert {row["marker"] for row in public.union(other)} == {"public", "other"}
    assert list(rows.values_list("body").union(rows.values_list("body"))) == [("",)]


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_aggregate_cannot_return_a_gated_field(alice):
    from tests.testapp.models import Post

    post = _post(title="aggregate secret")
    _grant(post.pk, alice, "viewer")
    qs = Post.objects.as_user(alice)

    with pytest.raises(PermissionDenied):
        qs.aggregate(secret=Min("title"))
    assert qs.aggregate(total=Count("pk")) == {"total": 1}
    assert qs.for_write().aggregate(secret=Min("title")) == {"secret": "aggregate secret"}


def test_no_read_gates_do_not_add_field_accessible_calls(alice, monkeypatch):
    install_schema(backend(), parse_zed(NO_READ_GATE_SCHEMA_TEXT))
    post = _post(title="plain")
    _post(title="hidden")
    _grant(post.pk, alice, "owner")
    active_backend = backend()
    actions: list[str] = []
    original_accessible = active_backend.accessible

    def counting_accessible(**kwargs: Any):
        actions.append(kwargs["action"])
        return original_accessible(**kwargs)

    monkeypatch.setattr(active_backend, "accessible", counting_accessible)

    with (
        override_settings(REBAC_FIELD_READ_MODE="redact"),
        CaptureQueriesContext(connection) as queries,
    ):
        assert [row.pk for row in Post.objects.as_user(alice)] == [post.pk]

    assert actions == []
    post_table = connection.ops.quote_name(Post._meta.db_table)
    row_queries = [query["sql"] for query in queries if f"FROM {post_table}" in query["sql"]]
    assert len(row_queries) == 1


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_redaction_scrubs_loaded_value_snapshot(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="snapshot secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    instance = Post.objects.as_user(alice).get(pk=post.pk)

    assert instance.title is None
    assert "title" not in instance._rebac_loaded_values


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_full_save_excludes_redacted_fields_from_the_update(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance.title is None
    instance.body = "safe update"
    instance.save()

    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "stored secret"
    assert fresh.body == "safe update"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_full_save_uses_dirty_fields_minus_redacted_fields(alice, bob):
    from tests.testapp.models import Post

    install_schema(backend(), parse_zed(WRITE_GATE_WITH_REDACTED_BODY_SCHEMA_TEXT))
    post = _post(title="visible title", body="stored secret")
    folder = _folder(name="new folder")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance.body is None
    instance.folder = folder
    instance.save()

    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "visible title"
    assert fresh.body == "stored secret"
    assert fresh.folder_id == folder.pk


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_bulk_update_still_works_when_read_gates_are_enabled(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="bulk title", body="original")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    count = Post.objects.as_user(alice).filter(pk=post.pk).update(body="bulk safe")

    assert count == 1
    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "bulk title"
    assert fresh.body == "bulk safe"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_full_save_skips_deferred_fields_when_redaction_narrows_update_fields(alice, bob):
    from tests.testapp.models import Post

    install_schema(backend(), parse_zed(WRITE_GATE_WITH_REDACTED_BODY_SCHEMA_TEXT))
    post = _post(title="deferred title", body="stored secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).only("id", "body", "folder").get(pk=post.pk)
    assert instance.body is None
    instance.folder = _folder(name="changed folder")
    instance.save()

    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "deferred title"
    assert fresh.body == "stored secret"
    assert fresh.folder_id == instance.folder_id


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_django_6_rejects_positional_save_before_redacted_fields_can_write(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance.title is None
    instance.body = "safe update"
    with pytest.raises(TypeError):
        instance.save(False, False, None, None)

    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "stored secret"
    assert fresh.body == "original"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_pickled_redacted_instance_preserves_write_safety_metadata(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance.title is None
    restored = pickle.loads(pickle.dumps(instance)).with_actor(alice)
    restored.body = "after pickle"
    restored.save()

    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert fresh.title == "stored secret"
    assert fresh.body == "after pickle"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_redacted_resource_id_attr_still_authorizes_writes_against_loaded_id(alice, bob):
    from tests.testapp.models import SluggedPost

    install_schema(backend(), parse_zed(SLUGGED_SCHEMA_TEXT))
    post = _slugged_post(slug="visible-id", title="old title")
    _grant_ref("blog/sluggedpost", "visible-id", bob, "owner")
    _grant_ref("blog/sluggedpost", "visible-id", alice, "viewer")
    _grant_ref("blog/sluggedpost", "visible-id", alice, "editor")

    instance = SluggedPost.objects.as_user(alice).get(pk=post.pk)
    assert instance.slug is None
    instance.title = "new title"
    instance.save()

    with sudo(reason="test.verify"):
        fresh = SluggedPost.objects.get(pk=post.pk)
    assert fresh.slug == "visible-id"
    assert fresh.title == "new title"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_explicit_save_of_redacted_field_fails_closed(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="stored secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance.title is None
    instance.title = "overwrite"

    with pytest.raises(PermissionDenied) as excinfo:
        instance.save(update_fields=["title"])
    assert "redacted" in str(excinfo.value)
    assert "title" in str(excinfo.value)


def test_instance_denied_read_fields_honours_caveat_context(alice):
    install_schema(backend(), parse_zed(CAVEAT_SCHEMA_TEXT))
    post = _post(title="conditional title")
    _grant(post.pk, alice, "viewer")
    _grant(
        post.pk,
        alice,
        "gated_reader",
        caveat_name="link_not_expired",
        caveat_context={"expires_at": FUTURE},
    )

    assert post.with_actor(alice).denied_read_fields(context={"now": PAST}) == frozenset()
    assert post.with_actor(alice).denied_read_fields(context={"now": FUTURE}) == frozenset(
        {"title"}
    )
    assert post.with_actor(alice).denied_read_fields() == frozenset({"title"})


def test_bulk_conditional_field_reads_fail_closed_by_default_and_can_flip(alice):
    from tests.testapp.models import Post

    install_schema(backend(), parse_zed(CAVEAT_SCHEMA_TEXT))
    post = _post(title="conditional title")
    _grant(post.pk, alice, "viewer")
    _grant(
        post.pk,
        alice,
        "gated_reader",
        caveat_name="link_not_expired",
        caveat_context={"expires_at": FUTURE},
    )

    with override_settings(REBAC_FIELD_READ_MODE="redact"):
        assert Post.objects.as_user(alice).get(pk=post.pk).title is None

    with override_settings(
        REBAC_FIELD_READ_MODE="redact",
        REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL=False,
    ):
        assert Post.objects.as_user(alice).get(pk=post.pk).title == "conditional title"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_sudo_queryset_skips_field_redaction(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="sudo visible")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    row = Post.objects.sudo(reason="test").on_field_deny("redact").get(pk=post.pk)

    assert row.title == "sudo visible"
    assert getattr(row, "_rebac_redacted_fields", frozenset()) == frozenset()


@override_settings(REBAC_FIELD_READ_MODE="raise")
def test_raise_mode_degrades_to_redact_until_descriptor_tier_lands(alice, bob):
    from tests.testapp.models import Post

    post = _post(title="redacted through raise")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")

    row = Post.objects.as_user(alice).get(pk=post.pk)

    assert row.title is None
    assert row._rebac_redacted_fields == frozenset({"title"})


def test_on_field_deny_rejects_unknown_modes():
    from tests.testapp.models import Post

    with pytest.raises(ValueError):
        Post.objects.on_field_deny(cast(Any, "explode"))


def test_on_field_deny_raise_surfaces_runtime_w008():
    from tests.testapp.models import Post

    with pytest.warns(RuntimeWarning, match="rebac.W008"):
        Post.objects.on_field_deny("raise")


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_bypass_subquery_reading_a_gated_column_does_not_block_the_outer_projection(alice, bob):
    """A column read inside a subquery with its own scope decision is not the
    outer row's projection: the outer row receives only what the subquery
    selects (0.24.0 attributed the inner alias's column to the outer query)."""
    from django.db.models import Case, Exists, OuterRef, Q, Subquery, Value, When

    from tests.testapp.models import Post

    post = _post(title="projected secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    flagged = Exists(
        Post.objects.sudo(reason="test.flag").filter(
            pk=OuterRef("pk"), title__startswith="projected"
        )
    )
    blocker = Subquery(
        Post.objects.sudo(reason="test.blocker")
        .filter(pk=OuterRef("pk"))
        .annotate(
            _blocker=Case(When(Q(title__isnull=False), then=Value(True)), default=Value(False))
        )
        .values("_blocker")[:1]
    )
    rows = list(
        Post.objects.as_user(alice)
        .annotate(flagged=flagged, blocked=blocker)
        .values("pk", "flagged", "blocked")
    )
    assert rows == [{"pk": post.pk, "flagged": True, "blocked": True}]


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_outer_row_read_of_a_gated_column_inside_a_subquery_fails_closed(alice, bob):
    from django.db.models import OuterRef, Subquery

    from tests.testapp.models import Post

    post = _post(title="projected secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    leak = Subquery(
        Post.objects.sudo(reason="test.leak").annotate(v=OuterRef("title")).values("v")[:1]
    )
    with pytest.raises(PermissionDenied) as excinfo:
        list(Post.objects.as_user(alice).annotate(x=leak).values("x"))
    assert "read__title" in str(excinfo.value)


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_actor_scoped_subquery_projecting_a_gated_column_fails_closed(alice, bob):
    """An actor-scoped subquery answers for its own projection."""
    from django.db.models import OuterRef, Subquery

    from tests.testapp.models import Post

    post = _post(title="projected secret")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    with pytest.raises(PermissionDenied) as excinfo:
        leak = Subquery(Post.objects.as_user(alice).filter(pk=OuterRef("pk")).values("title")[:1])
        list(Post.objects.as_user(alice).annotate(x=leak).values("x"))
    assert "read__title" in str(excinfo.value)
