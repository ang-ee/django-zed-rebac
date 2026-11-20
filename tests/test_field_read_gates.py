"""Field-level read enforcement.

``read__<field>`` is a normal permission name in the schema. These tests pin
the model-layer redaction behavior that consumes those permissions for every
transport, not just GraphQL.
"""

from __future__ import annotations

import pickle
from typing import Any, cast

import pytest
from django.core.files.base import ContentFile
from django.db import connection
from django.db.models import CharField, Count, F, FilteredRelation, Min, Value
from django.db.models.expressions import RawSQL
from django.db.models.functions import Upper
from django.db.models.query_utils import DeferredAttribute
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
from rebac.field_visibility import backend_schema, mark_denied_fields, redacted_fields
from rebac.schema import parse_zed
from tests.backend_setup import UNUSABLE_SCHEMA_ACCESSORS, atomic_source_write, install_schema
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


def test_redacted_fields_is_empty_for_an_unmarked_model():
    from django.contrib.auth import get_user_model

    assert redacted_fields(get_user_model()()) == frozenset()
    assert redacted_fields(Post(title="visible")) == frozenset()


def test_redacted_fields_is_an_immutable_accumulated_snapshot():
    post = Post(title="secret", body="also secret")
    mark_denied_fields(post, ("title",), mode="redact")
    first = redacted_fields(post)
    assert isinstance(first, frozenset)
    assert first == frozenset({"title"})
    mark_denied_fields(post, ("body",), mode="omit")
    assert first == frozenset({"title"})
    assert redacted_fields(post) == frozenset({"title", "body"})


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
    assert redacted_fields(rows[0]) == frozenset()
    assert rows[1].title is None
    assert redacted_fields(rows[1]) == frozenset({"title"})


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


@pytest.mark.parametrize(("accessor", "error"), UNUSABLE_SCHEMA_ACCESSORS)
def test_backend_schema_refuses_an_unusable_accessor(monkeypatch, accessor, error):
    """The accessor both field gates share raises; it never answers "no gates"."""
    monkeypatch.setattr(backend(), "schema", accessor)

    with pytest.raises(error):
        backend_schema()


@pytest.mark.parametrize(("accessor", "error"), UNUSABLE_SCHEMA_ACCESSORS)
def test_read_gate_fails_closed_without_a_usable_schema(alice, monkeypatch, accessor, error):
    """A viewer's fetch would redact ``title``; without the schema it raises, not
    leaks, and the gates are read before the rows, so none is cached unredacted."""
    post = _post(title="stored secret")
    _grant(post.pk, alice, "viewer")
    monkeypatch.setattr(backend(), "schema", accessor)
    rows = Post.objects.as_user(alice)

    with override_settings(REBAC_FIELD_READ_MODE="redact"), pytest.raises(error):
        list(rows)
    assert rows._result_cache is None


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
def test_full_save_refuses_a_redacted_field_given_a_value(alice, bob):
    """A save that names no fields writes no redacted field: one given a value is refused before
    any statement, where the save left it out and reported success. A None given to it cannot be
    told from the redacted value, and is left out."""
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")

    instance = Post.objects.as_user(alice).get(pk=post.pk)
    instance.title = "overwrite"
    instance.body = "edited"
    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(
            PermissionDenied, match=r"has title hidden .*, and holds a value given to it"
        ):
            instance.save()
    assert captured.captured_queries == []
    instance.title = None
    instance.save()

    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert (fresh.title, fresh.body) == ("stored secret", "edited")


@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("form", ["insert", "ignore_conflicts", "update_conflicts"])
@pytest.mark.parametrize("queryset", ["actor", "sudo", "base manager"])
def test_a_redacted_row_is_not_bulk_created(alice, bob, queryset, form):
    """``bulk_create()`` of a redacted row would store its redacted None as data: refused before
    any statement, in every form and through any manager."""
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")
    instance = Post.objects.as_user(alice).get(pk=post.pk)
    rows = {
        "actor": Post.objects.as_user(alice),
        "sudo": Post.objects.sudo(reason="test.copy"),
        "base manager": Post._base_manager.all(),
    }[queryset]
    kwargs: dict[str, Any] = {
        "insert": {},
        "ignore_conflicts": {"ignore_conflicts": True},
        "update_conflicts": {
            "update_conflicts": True,
            "unique_fields": ["id"],
            "update_fields": ["body"],
        },
    }[form]
    refused = rf"testapp\.Post row {post.pk} has title hidden .*bulk_create\(\) would insert"
    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(PermissionDenied, match=refused):
            rows.bulk_create([instance], **kwargs)
    assert captured.captured_queries == []
    with sudo(reason="test.verify"):
        assert list(Post.objects.values_list("title", "body")) == [("stored secret", "original")]


FILED_DOC_SCHEMA_TEXT = """
definition auth/user {}

definition test/fileddoc {
    relation editor: auth/user
    permission read = editor
    permission write = editor
    permission read__attachment = nil
    permission read__picture = nil
    permission read__content_type = nil
    permission read__object_id = nil
    permission read__kind = nil
    permission read__note = nil
}
"""


class _Unset:
    """What the note field's descriptor makes of the None it reads."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Unset)

    __hash__ = object.__hash__


class _NoteDescriptor(DeferredAttribute):
    """Stamps the shown ``stamp`` column on every assignment, and wraps a None it reads, for
    which it reads the row's ``_state``: a bare instance of the model has none."""

    def __get__(self, instance: Any, cls: Any = None) -> Any:
        if instance is None:
            return self
        value = super().__get__(instance, cls)
        if value is None:
            instance._state  # noqa: B018
            value = instance.__dict__[self.field.attname] = _Unset()
        return value

    def __set__(self, instance: Any, value: Any) -> None:
        instance.__dict__[self.field.attname] = value
        instance.__dict__["stamp"] = f"stamped {value}"


class _NoteField(CharField):  # type: ignore[type-arg]
    descriptor_class = _NoteDescriptor


@pytest.fixture
def filed_doc(transactional_db, alice):
    """Document 1, which alice edits: she is denied its file, its image (whose width and height
    she reads), the parts of its generic relation, its kind (which the model's ``__setattr__``
    copies, upper-cased, to an attribute that is no field) and its note (whose descriptor stamps
    the shown ``stamp`` column). The model and its table are the test's own: an ImageField
    cannot be declared without Pillow."""
    from django.contrib.contenttypes.fields import GenericForeignKey
    from django.contrib.contenttypes.models import ContentType
    from django.db import models
    from django.test.utils import isolate_apps

    from rebac import RebacMixin

    with isolate_apps("tests.testapp"):

        class FiledDoc(RebacMixin, models.Model):
            name = models.CharField(max_length=20, default="")
            attachment = models.FileField(null=True, blank=True, upload_to="docs/")
            picture = models.ImageField(
                null=True, upload_to="pictures/", width_field="width", height_field="height"
            )
            width = models.IntegerField(null=True)
            height = models.IntegerField(null=True)
            content_type = models.ForeignKey(
                ContentType, null=True, on_delete=models.CASCADE, related_name="+"
            )
            object_id = models.PositiveIntegerField(null=True)
            about = GenericForeignKey("content_type", "object_id")
            kind = models.CharField(max_length=20, null=True)
            note = _NoteField(max_length=20, null=True)
            stamp = models.CharField(max_length=20, null=True)

            class Meta:
                app_label = "testapp"
                rebac_resource_type = "test/fileddoc"

            def __setattr__(self, name: str, value: Any) -> None:
                if name == "kind":
                    super().__setattr__("kind_upper", value.upper() if value else None)
                super().__setattr__(name, value)

        with connection.schema_editor() as editor:
            editor.create_model(FiledDoc)
        try:
            install_schema(backend(), parse_zed(FILED_DOC_SCHEMA_TEXT))
            kind = ContentType.objects.get_for_model(Post)
            FiledDoc._base_manager.bulk_create(
                [
                    FiledDoc(
                        id=1,
                        name="doc",
                        attachment="docs/a.txt",
                        picture="pictures/a.png",
                        width=10,
                        height=20,
                        content_type=kind,
                        object_id=7,
                        kind="secret",
                        note="private",
                        stamp="s1",
                    )
                ]
            )
            _grant_ref("test/fileddoc", "1", alice, "editor")
            yield FiledDoc, kind
        finally:
            with connection.schema_editor() as editor:
                editor.delete_model(FiledDoc)


def _stored_doc(model: Any) -> tuple[Any, ...]:
    return tuple(
        model._base_manager.values_list(
            "name",
            "attachment",
            "picture",
            "width",
            "height",
            "content_type_id",
            "object_id",
            "kind",
            "note",
            "stamp",
        ).get(pk=1)
    )


def _as_loaded(kind: Any, *, name: str = "doc") -> tuple[Any, ...]:
    """Document 1 as stored by the fixture, with ``name``."""
    return (name, "docs/a.txt", "pictures/a.png", 10, 20, kind.pk, 7, "secret", "private", "s1")


# A redacted field read as Django gives it, through its descriptor.
READS: dict[str, Any] = {
    "the file": lambda row: bool(row.attachment),
    "the image": lambda row: bool(row.picture),
    "the foreign key": lambda row: row.content_type,
    "the generic relation": lambda row: row.about,
}


@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("read", sorted(READS))
def test_reading_a_redacted_field_gives_it_no_value(filed_doc, alice, read):
    """A file field's descriptor writes a FieldFile with no name back onto the row when its None
    is read; a relation caches None. Each still holds what it was hidden with, so the save of the
    changed name writes the name alone, and ``check_save()`` passes."""
    model, kind = filed_doc
    row = model.objects.as_user(alice).get(pk=1)
    READS[read](row)
    row.name = "renamed"
    assert row.check_save().allowed
    row.save()
    assert _stored_doc(model) == _as_loaded(kind, name="renamed")


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_hiding_runs_the_models_code_and_puts_back_what_it_changed_in_shown_columns(
    filed_doc, alice
):
    """Each field is hidden through ``setattr``, so the model's own ``__setattr__`` and the field's
    descriptor run, as on any assignment: the upper-cased copy of the kind, no field, is cleared
    with it. A shown column the hide changed is put back as loaded: the image's width and height,
    which its descriptor set to None, and the stamp the note's descriptor rewrote. A save of the
    changed name writes the name alone."""
    model, kind = filed_doc
    row = model.objects.as_user(alice).get(pk=1)
    assert redacted_fields(row) == {
        "attachment",
        "picture",
        "content_type",
        "object_id",
        "kind",
        "note",
    }
    assert (row.kind, row.kind_upper, vars(row)["note"]) == (None, None, None)
    assert (row.width, row.height, row.stamp) == (10, 20, "s1")
    row.name = "renamed"
    row.save()
    assert _stored_doc(model) == _as_loaded(kind, name="renamed")


# A value given to a redacted field.
GIVEN: dict[str, Any] = {
    "a file name": lambda row, _kind: setattr(row, "attachment", "docs/b.txt"),
    "a name on the file read": lambda row, _kind: setattr(row.attachment, "name", "docs/b.txt"),
    "a content type": lambda row, kind: setattr(row, "content_type", kind),
    "a nameless file": lambda row, _kind: setattr(row, "attachment", ContentFile(b"new data")),
    "the note read": lambda row, _kind: row.note,
}


@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("given", sorted(GIVEN))
def test_a_value_given_to_a_redacted_field_is_refused(filed_doc, alice, given):
    """Anything put in a redacted field but its placeholder is a value given to it, refused before
    any statement: a nameless ``ContentFile`` compares equal, by name, to the ``FieldFile`` with no
    name, and is not one. The note's descriptor reads the row's state to wrap the None it reads,
    and a bare instance has none: what it wrapped on the row reads as a value given too."""
    model, kind = filed_doc
    row = model.objects.as_user(alice).get(pk=1)
    GIVEN[given](row, kind)
    row.name = "renamed"
    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(PermissionDenied, match="holds a value given to it"):
            row.save()
    assert captured.captured_queries == []
    assert _stored_doc(model) == _as_loaded(kind)


PAIR_SCHEMA_TEXT = """
definition auth/user {}

definition test/pairdoc {
    relation editor: auth/user
    permission read = editor
    permission write = editor
    permission read__a = nil
    permission read__b = nil
    permission read__picture = nil
}
"""


@pytest.fixture(params=["notes first", "notes last"])
def pair_doc(request, transactional_db, alice):
    """Document 1: ``a``, ``b`` and an image hidden; ``a_note``, ``b_note``, ``width`` and ``height``
    shown. The model's ``__setattr__`` writes ``b_note`` from ``a`` and ``a_note`` from ``b``, so
    as Django builds a row it copies the hidden values into the shown notes when they are
    declared first, and as the library hides ``a`` and ``b`` it rewrites both notes."""
    from django.db import models
    from django.test.utils import isolate_apps

    from rebac import RebacMixin

    def notes() -> dict[str, Any]:
        return {
            "a_note": models.CharField(max_length=40, null=True),
            "b_note": models.CharField(max_length=40, null=True),
        }

    def hidden() -> dict[str, Any]:
        return {
            "a": models.CharField(max_length=40, null=True),
            "b": models.CharField(max_length=40, null=True),
        }

    def __setattr__(self: Any, name: str, value: Any) -> None:
        if name == "a":
            models.Model.__setattr__(self, "b_note", f"from a: {value}")
        if name == "b":
            models.Model.__setattr__(self, "a_note", f"from b: {value}")
        models.Model.__setattr__(self, name, value)

    with isolate_apps("tests.testapp"):
        first, last = (notes(), hidden()) if request.param == "notes first" else (hidden(), notes())
        attrs: dict[str, Any] = {"__module__": __name__, **first, **last}
        attrs["picture"] = models.ImageField(
            null=True, upload_to="pictures/", width_field="width", height_field="height"
        )
        attrs["width"] = models.IntegerField(null=True)
        attrs["height"] = models.IntegerField(null=True)
        attrs["Meta"] = type(
            "Meta", (), {"app_label": "testapp", "rebac_resource_type": "test/pairdoc"}
        )
        attrs["__setattr__"] = __setattr__
        model = cast(Any, type("PairDoc", (RebacMixin, models.Model), attrs))
        with connection.schema_editor() as editor:
            editor.create_model(model)
        try:
            install_schema(backend(), parse_zed(PAIR_SCHEMA_TEXT))
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {model._meta.db_table} (id, a, b, a_note, b_note, picture, width, "
                    "height) VALUES (1, 'secret-a', 'secret-b', 'stored a_note', 'stored b_note', "
                    "'pictures/x.png', 10, 20)"
                )
            _grant_ref("test/pairdoc", "1", alice, "editor")
            yield model
        finally:
            with connection.schema_editor() as editor:
                editor.delete_model(model)


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_hiding_puts_back_what_the_database_returned_for_a_shown_column(pair_doc, alice):
    """A shown column the hide changed is put back as the database returned it, never as model
    code derived it while Django built the row (from the hidden values, when the notes come
    first): the notes and the image's width and height are as stored, and a save writes none of
    them."""
    row = pair_doc.objects.as_user(alice).get(pk=1)
    assert redacted_fields(row) == {"a", "b", "picture"}
    assert (row.a, row.b) == (None, None)
    assert (row.a_note, row.b_note, row.width, row.height) == (
        "stored a_note",
        "stored b_note",
        10,
        20,
    )
    row.save()
    assert pair_doc._base_manager.values_list("a", "b", "a_note", "b_note", "width", "height").get(
        pk=1
    ) == ("secret-a", "secret-b", "stored a_note", "stored b_note", 10, 20)


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_deferred_shown_column_the_hide_changed_is_deferred_again(pair_doc, alice):
    """``a_note`` and ``width`` were not read; model code filled them as Django built the row
    (``a_note`` from the hidden ``b`` when the notes come first, ``width`` by Django's image
    field, which loads it), and the hide changed them: they are deferred again, and a read loads
    what is stored."""
    row = pair_doc.objects.as_user(alice).defer("a_note", "width").get(pk=1)
    assert {"a_note", "width"} <= row.get_deferred_fields()
    assert (row.a_note, row.width, row.height, row.b_note) == (
        "stored a_note",
        10,
        20,
        "stored b_note",
    )
    assert (row.a, row.b) == (None, None)


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_key_given_in_another_form_of_the_same_value_is_not_moved(alice, bob):
    """The key compared as the field prepares it: ``"1"`` on an integer key is the row's own key,
    so the save updates it and is no insert."""
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")
    instance = Post.objects.as_user(alice).get(pk=post.pk)
    instance.pk = str(post.pk)
    instance.body = "edited"
    instance.save()
    with sudo(reason="test.verify"):
        assert list(Post.objects.values_list("pk", "title", "body")) == [
            (post.pk, "stored secret", "edited")
        ]


@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize(
    "copy",
    ["key None, marked as being added", "key kept, marked as being added", "key None", "forced"],
)
def test_a_redacted_row_is_not_copied(alice, bob, copy):
    """Saving a redacted row as a new one would store its redacted None as data: refused before
    any statement, where the copy idiom stored a row with the title NULL. A new row built from
    values the caller chose has nothing hidden and is saved as any."""
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")
    instance = Post.objects.as_user(alice).get(pk=post.pk).sudo(reason="test.copy")
    if copy != "key kept, marked as being added":
        instance.pk = None
    instance._state.adding = copy.endswith("marked as being added")
    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(PermissionDenied, match="would insert it as a new row"):
            instance.save(force_insert=copy == "forced")
    assert captured.captured_queries == []
    with sudo(reason="test.verify"):
        assert Post.objects.count() == 1
        Post(body=instance.body, title="chosen").save()
        assert Post.objects.count() == 2


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
def test_a_redacted_field_is_never_written_from_the_row_that_hid_it(alice, bob):
    """The redacted ``title`` holds None on alice's row. ``bulk_update()`` naming it is refused
    before any statement, under her and under a bypass, and so is ``save(update_fields=...)``
    under the row's own bypass; naming only ``body`` writes. Until this release each of the
    three stored None over the title."""
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")
    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance.title is None
    instance.body = "bulk safe"
    for queryset in (Post.objects.as_user(alice), Post.objects.sudo(reason="test.bulk")):
        with CaptureQueriesContext(connection) as captured:
            with pytest.raises(PermissionDenied, match="has title hidden"):
                queryset.bulk_update([instance], ["title", "body"])
        assert captured.captured_queries == []
    with pytest.raises(PermissionDenied, match="has title hidden"):
        instance.sudo(reason="test.save").save(update_fields=["title", "body"])
    with sudo(reason="test.verify"):
        assert Post.objects.get(pk=post.pk).title == "stored secret"
    assert Post.objects.as_user(alice).bulk_update([instance], ["body"]) == 1
    with sudo(reason="test.verify"):
        fresh = Post.objects.get(pk=post.pk)
    assert (fresh.title, fresh.body) == ("stored secret", "bulk safe")


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_update_or_create_refuses_a_field_its_own_read_hides(alice, bob):
    """``update_or_create`` reads its row under the queryset's actor and saves the fields it was
    given: a field that read redacts is refused, whatever value was given for it."""
    from tests.testapp.models import Post

    post = _post(title="stored secret", body="original")
    _grant(post.pk, bob, "owner")
    _grant(post.pk, alice, "viewer")
    _grant(post.pk, alice, "editor")
    with pytest.raises(PermissionDenied, match="has title hidden"):
        Post.objects.as_user(alice).update_or_create(pk=post.pk, defaults={"title": None})
    with sudo(reason="test.verify"):
        assert Post.objects.get(pk=post.pk).title == "stored secret"


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


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_the_projection_guard_runs_once_per_evaluation(alice, monkeypatch):
    from rebac.managers import RebacQuerySet
    from tests.testapp.models import Post

    post = _post(title="guarded once")
    _grant(post.pk, alice, "viewer")
    calls = []
    guard = RebacQuerySet._guard_projected_field_reads

    def counting(self, *args, **kwargs):
        calls.append(self)
        return guard(self, *args, **kwargs)

    monkeypatch.setattr(RebacQuerySet, "_guard_projected_field_reads", counting)
    rows = Post.objects.as_user(alice)
    # ``list()`` asks for the length, then iterates; both read the one result.
    assert [row.pk for row in list(rows)] == [post.pk]
    assert len(rows) == 1
    assert rows
    assert len(calls) == 1


@pytest.mark.pg_delta
@pytest.mark.parametrize("field_count", (1, 2, 3))
@pytest.mark.parametrize("row_count", (1, 5))
def test_field_visibility_statement_growth_is_per_distinct_permission(
    alice, field_count, row_count
):
    from rebac.field_visibility import visible_id_sets

    fields = ("title", "body", "folder")[:field_count]
    expressions = ("owner", "editor", "viewer")[:field_count]
    gates = "\n    ".join(
        f"permission read__{field} = {expression}"
        for field, expression in zip(fields, expressions, strict=True)
    )
    install_schema(
        backend(),
        parse_zed(SCHEMA_TEXT.replace("permission read__title = owner", gates)),
    )
    posts = [_post(title=f"post {index}") for index in range(row_count)]
    for post in posts:
        _grant(post.pk, alice, "viewer")
    ids = tuple(str(post.pk) for post in posts)
    actor = SubjectRef.of("auth/user", str(alice.pk))
    with CaptureQueriesContext(connection) as queries:
        visible = visible_id_sets(
            model=Post, actor=actor, fields=frozenset(fields), resource_ids=ids
        )
    assert set(visible) == set(fields)
    assert len(queries) == field_count  # 1, 2, 3 permissions; constant in row_count.


@pytest.mark.pg_delta
@pytest.mark.parametrize("field_count", (1, 2, 3))
def test_field_visibility_equal_permissions_share_one_statement(alice, field_count):
    from rebac.field_visibility import visible_id_sets

    fields = ("title", "body", "folder")[:field_count]
    gates = "\n    ".join(f"permission read__{field} = read_private" for field in fields)
    install_schema(
        backend(),
        parse_zed(
            SCHEMA_TEXT.replace(
                "permission read__title = owner",
                f"permission read_private = owner\n    {gates}",
            )
        ),
    )
    posts = [_post(title=f"post {index}") for index in range(5)]
    actor = SubjectRef.of("auth/user", str(alice.pk))
    ids = tuple(str(post.pk) for post in posts)
    with CaptureQueriesContext(connection) as queries:
        visible = visible_id_sets(
            model=Post, actor=actor, fields=frozenset(fields), resource_ids=ids
        )
    assert all(not allowed for allowed in visible.values())
    assert len(queries) == 1  # Constant in the number of fields sharing read_private.
