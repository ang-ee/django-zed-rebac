"""Field read gates: a filter or an ordering on a gated column reveals it (issue #40)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db.models import (
    BooleanField,
    Count,
    Exists,
    F,
    FilteredRelation,
    Max,
    OuterRef,
    Q,
    Subquery,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Abs, Lower
from django.test import override_settings

from rebac import (
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    actor_context,
    backend,
    sudo,
    to_subject_ref,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db

SCHEMA = """
definition auth/user {}

definition test/roleanchor {
    relation member: auth/user
}

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
    relation admin: test/roleanchor // rebac:const=admin
    relation folder: blog/folder // rebac:field=folder
    permission read = ((owner + editor) + viewer) + admin->member
    permission write = (owner + editor) + admin->member
    permission delete = (owner + editor) + admin->member
    permission read__title = owner + admin->member
}
"""


@pytest.fixture(autouse=True)
def _schema(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA))
    yield
    reset_backend()


def _grant(resource_type, pk, user, relation):
    backend().write_relationships(
        [RelationshipTuple(ObjectRef(resource_type, str(pk)), relation, to_subject_ref(user))]
    )


@pytest.fixture
def people(db):
    users = get_user_model().objects
    return {name: users.create(username=name) for name in ("alice", "bob", "carol")}


@pytest.fixture
def rows(people):
    """alice owns one post and views the other; bob edits both; carol is an admin."""
    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="secret folder")
        own = Post.objects.create(title="alpha memo", folder=folder)
        hidden = Post.objects.create(title="secret plan", folder=folder)
        hidden.collections.add(folder)
    _grant("blog/post", own.pk, people["alice"], "owner")
    _grant("blog/post", hidden.pk, people["alice"], "viewer")
    _grant("blog/folder", folder.pk, people["alice"], "viewer")
    for post in (own, hidden):
        _grant("blog/post", post.pk, people["bob"], "editor")
    backend().write_relationships(
        [
            RelationshipTuple(
                ObjectRef("test/roleanchor", "admin"), "member", to_subject_ref(people["carol"])
            )
        ]
    )
    return folder, own, hidden


def surfaces(alice):
    rows = Post.objects.as_user(alice)
    folders = Folder.objects.as_user(alice)
    secret = Q(title__startswith="secret")
    return {
        "filter": lambda: list(rows.filter(secret)),
        "exclude": lambda: list(rows.exclude(secret)),
        "get": lambda: rows.get(title="secret plan"),
        "count": lambda: rows.filter(secret).count(),
        "exists": lambda: rows.filter(secret).exists(),
        "projection": lambda: list(rows.filter(secret).values_list("pk", flat=True)),
        "aggregate": lambda: rows.filter(secret).aggregate(n=Count("pk")),
        "order_by": lambda: list(rows.order_by("title")),
        "order_by descending": lambda: list(rows.order_by("-title")),
        "order_by expression": lambda: list(rows.order_by(Lower("title").desc())),
        "distinct": lambda: list(rows.order_by("title").distinct("title")),
        "an alias": lambda: list(rows.alias(t=F("title")).filter(t="secret plan")),
        "a joined model": lambda: list(rows.filter(folder__name__startswith="secret")),
        "a joined ordering": lambda: list(rows.order_by("folder__name")),
        "a filtered relation": lambda: list(
            rows.annotate(
                f=FilteredRelation("folder", condition=Q(folder__name="secret folder"))
            ).filter(f__isnull=False)
        ),
        "an actor-scoped subquery": lambda: list(
            rows.filter(Exists(folders.filter(pk=OuterRef("folder"), name="secret folder")))
        ),
        "a queryset given to a lookup": lambda: rows.filter(pk__in=rows.filter(secret)).count(),
        "a sliced queryset given to a lookup": lambda: list(
            rows.filter(pk__in=rows.order_by("title").values("pk")[:1])
        ),
        "a projection given to a lookup": lambda: rows.filter(
            body__in=rows.values("title")
        ).count(),
        "an eagerly scoped queryset given to a lookup": lambda: rows.filter(
            pk__in=rows.scoped().filter(secret)
        ).count(),
        "an eagerly scoped subquery's projection": lambda: list(
            rows.annotate(
                t=Subquery(rows.scoped().filter(pk=OuterRef("pk")).values("title")[:1])
            ).values_list("pk", flat=True)
        ),
        "an exclusion across a to-many relation": lambda: list(
            rows.exclude(collections__name__startswith="secret")
        ),
        "an exclusion across a reverse relation": lambda: list(
            folders.exclude(posts__title__startswith="secret")
        ),
        "a count of distinct values": lambda: rows.values("title").distinct().count(),
        "a grouping": lambda: list(
            rows.values("title").annotate(n=Count("pk")).values_list("n", flat=True)
        ),
        "a grouping tested": lambda: (
            rows.values("title").annotate(n=Count("pk")).filter(n__gt=1).exists()
        ),
        "a sliced aggregate": lambda: rows.order_by("title")[:1].aggregate(m=Max("pk")),
        "a sliced distinct count": lambda: (
            rows.values("body").order_by("title").distinct()[:10].count()
        ),
        "hand-written SQL in a filter": lambda: list(rows.extra(where=["1 = 1"])),
        "RawSQL in a filter": lambda: list(
            rows.filter(RawSQL("1 = 1", [], output_field=BooleanField()))
        ),
        "an extra ordering by name": lambda: list(rows.extra(order_by=["-title"])),
        "hand-written SQL in an ordering": lambda: list(
            rows.extra(order_by=[f"{Post._meta.db_table}.body"])
        ),
        "latest": lambda: rows.latest("title"),
        "the left operand of a union": lambda: list(
            rows.filter(secret).values("pk").union(rows.filter(body="x").values("pk"))
        ),
        "the right operand of a union": lambda: list(
            rows.filter(body="x").values("pk").union(rows.filter(secret).values("pk"))
        ),
        "a difference": lambda: list(
            rows.values("pk").difference(rows.filter(secret).values("pk"))
        ),
        "a sliced operand's ordering": lambda: list(
            rows.values("pk").union(rows.order_by("title").values("pk")[:1])
        ),
        "a union counted": lambda: rows.filter(body="x").union(rows.filter(secret)).count(),
    }


SURFACES = [
    "filter",
    "exclude",
    "get",
    "count",
    "exists",
    "projection",
    "aggregate",
    "order_by",
    "order_by descending",
    "order_by expression",
    "distinct",
    "an alias",
    "a joined model",
    "a joined ordering",
    "a filtered relation",
    "an actor-scoped subquery",
    "a queryset given to a lookup",
    "a sliced queryset given to a lookup",
    "a projection given to a lookup",
    "an eagerly scoped queryset given to a lookup",
    "an eagerly scoped subquery's projection",
    "an exclusion across a to-many relation",
    "an exclusion across a reverse relation",
    "a count of distinct values",
    "a grouping",
    "a grouping tested",
    "a sliced aggregate",
    "a sliced distinct count",
    "hand-written SQL in a filter",
    "RawSQL in a filter",
    "an extra ordering by name",
    "hand-written SQL in an ordering",
    "latest",
    "the left operand of a union",
    "the right operand of a union",
    "a difference",
    "a sliced operand's ordering",
    "a union counted",
]


@pytest.mark.parametrize("mode", ["redact", "omit"])
@pytest.mark.parametrize("surface", SURFACES)
def test_a_clause_that_reads_a_gated_column_is_refused(people, rows, mode, surface):
    # alice may read the title of one post and not of the other.
    with override_settings(REBAC_FIELD_READ_MODE=mode):
        with pytest.raises(PermissionDenied):
            surfaces(people["alice"])[surface]()


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_the_default_ordering_counts(people, rows, monkeypatch):
    monkeypatch.setattr(Post._meta, "ordering", ["title"])
    listed = Post.objects.as_user(people["alice"])
    with pytest.raises(PermissionDenied):
        list(listed)
    with pytest.raises(PermissionDenied):
        Post.objects.as_user(people["bob"]).filter(pk__in=listed).count()
    assert len(list(listed.order_by("pk"))) == 2
    # order_by() clears the default; a grouped query drops it, as Django does.
    assert len(list(listed.order_by())) == 2
    assert [row["n"] for row in listed.values("folder").annotate(n=Count("pk"))] == [2]
    assert listed.count() == 2


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_an_ordering_by_a_relation_is_its_default_ordering(people, rows, monkeypatch):
    monkeypatch.setattr(Folder._meta, "ordering", ["name"])
    listed = Post.objects.as_user(people["alice"])
    with pytest.raises(PermissionDenied, match="read__name"):
        list(listed.order_by("folder"))
    # Its key column is not the related model's default ordering.
    assert len(list(listed.order_by("folder_id"))) == 2


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_the_rows_a_write_selects_are_guarded_too(people, rows):
    # bob may write both posts and read neither title.
    edited = Post.objects.as_user(people["bob"])
    with pytest.raises(PermissionDenied):
        edited.filter(title="secret plan").update(body="found")
    with pytest.raises(PermissionDenied):
        edited.filter(title="secret plan").delete()
    assert edited.filter(body="").update(body="ok") == 2
    with sudo(reason="test.verify"):
        assert Post.objects.count() == 2


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_refused_evaluation_does_not_unguard_the_queryset(people, rows):
    listed = Post.objects.as_user(people["alice"])
    inner = listed.filter(pk=OuterRef("pk"), title__startswith="secret")
    with pytest.raises(PermissionDenied):
        listed.filter(title__startswith="secret").exists()
    probe = listed.filter(title__startswith="secret")
    with pytest.raises(PermissionDenied):
        probe.count()
    # The refused queryset now carries its scope; as a subquery it is checked all the same.
    with pytest.raises(PermissionDenied):
        list(listed.filter(pk__in=probe))
    with pytest.raises(PermissionDenied):
        list(listed.annotate(x=Exists(inner)).values_list("x", flat=True))


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_watched_update_is_refused_before_its_rows_are_selected(people, rows):
    # alice cannot write the hidden post: had its rows been gated first, the
    # refusal would say whether the title matched.
    with sudo(reason="test.fixture"):
        other = Folder.objects.create(name="other")
    listed = Post.objects.as_user(people["alice"])
    for title in ("secret plan", "no such title"):
        with pytest.raises(PermissionDenied, match="Cannot filter or order by gated field"):
            listed.filter(title=title).update(folder=other)


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_subquery_ordered_by_a_reference_to_its_outer_query(people, rows):
    folders = Folder.objects.as_user(people["alice"]).annotate(
        first=Subquery(
            Post.objects.as_user(people["alice"])
            .filter(folder=OuterRef("pk"))
            .annotate(d=Abs(F("pk") - OuterRef("pk")))
            .order_by("d")
            .values("pk")[:1]
        )
    )
    assert len(list(folders)) == 1


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_clauses_on_other_columns_and_bypasses_are_not_refused(people, rows):
    folder, own, hidden = rows
    alice = people["alice"]
    listed = Post.objects.as_user(alice)
    assert {post.pk for post in listed.filter(body="").order_by("-pk")} == {own.pk, hidden.pk}
    assert listed.filter(folder=folder).count() == 2
    assert listed.filter(pk=hidden.pk).exists()
    # A bypass subquery hands the outer row only its boolean.
    bypass = Folder.objects.sudo(reason="test.subquery")
    assert listed.filter(Exists(bypass.filter(pk=OuterRef("folder"), name="secret folder")))
    # A pinned actor outranks ambient sudo; the queryset's own sudo bypasses.
    with sudo(reason="test.sudo"):
        assert Post.objects.filter(title="secret plan").count() == 1
    assert listed.sudo(reason="test.sudo").filter(title="secret plan").count() == 1
    with override_settings(REBAC_FIELD_READ_MODE="allow"):
        assert listed.filter(title="secret plan").count() == 1


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_an_actor_who_reads_the_field_on_every_row_is_not_refused(people, rows):
    _folder, _own, hidden = rows
    admin = Post.objects.as_user(people["carol"])
    assert [post.pk for post in admin.filter(title="secret plan")] == [hidden.pk]
    assert [post.title for post in admin.order_by("-title")] == ["secret plan", "alpha memo"]
    assert list(admin.order_by("title").values_list("title", flat=True)) == [
        "alpha memo",
        "secret plan",
    ]
    # The folder's name is not the admin's to read: the post's admin role is not the folder's.
    with pytest.raises(PermissionDenied):
        list(admin.filter(folder__name="secret folder"))


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_an_exclusion_on_the_row_is_not_every_row(people, rows):
    """A grant that holds without the row can still be taken from one row."""
    _folder, own, hidden = rows
    reset_backend()
    install_schema(
        backend(),
        parse_zed(
            SCHEMA.replace(
                "    relation folder: blog/folder // rebac:field=folder\n",
                "    relation folder: blog/folder // rebac:field=folder\n"
                "    relation blocked: auth/user\n",
            ).replace(
                "permission read__title = owner + admin->member",
                "permission read__title = (owner + admin->member) - blocked",
            )
        ),
    )
    _grant("blog/post", hidden.pk, people["carol"], "blocked")
    admin = Post.objects.as_user(people["carol"])
    assert {post.pk: post.title for post in admin} == {own.pk: "alpha memo", hidden.pk: None}
    with pytest.raises(PermissionDenied):
        list(admin.filter(title="secret plan"))


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_subquery_does_not_answer_for_the_outer_row(people, rows):
    """An OuterRef column is the enclosing query's: its own guard decides it."""
    alice = people["alice"]

    def matching():
        return Exists(
            Folder.objects.as_user(alice).filter(pk=OuterRef("folder"), kind=OuterRef("title"))
        )

    def selected():
        return Subquery(
            Folder.objects.as_user(alice)
            .filter(pk=OuterRef("folder"))
            .annotate(o=OuterRef("title"))
            .values("o")[:1]
        )

    bypass = Post.objects.sudo(reason="test.outer")
    assert bypass.filter(~matching()).count() == 2
    assert sorted(bypass.annotate(t=selected()).values_list("t", flat=True)) == [
        "alpha memo",
        "secret plan",
    ]
    # carol reads every title; alice's subquery compares the outer row's only.
    assert Post.objects.as_user(people["carol"]).filter(~matching()).count() == 2
    # Under alice the outer query reads the title she may not filter on.
    with pytest.raises(PermissionDenied):
        Post.objects.as_user(alice).filter(~matching()).count()


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_scoped_queryset_keeps_the_actor_it_was_scoped_with(people, rows):
    with actor_context(to_subject_ref(people["alice"])):
        evaluated = Post.objects.filter(body="")
        assert len(list(evaluated)) == 2
        refused = Post.objects.filter(title__startswith="secret")
        with pytest.raises(PermissionDenied):
            list(refused)
    # Embedded after the context closed: alice's scope and alice's gates.
    bypass = Post.objects.sudo(reason="test.outer")
    assert bypass.filter(Exists(evaluated)).count() == 2
    with pytest.raises(PermissionDenied):
        bypass.filter(Exists(refused)).count()


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_count_reads_the_selection_only_of_distinct_or_grouped_rows(people, rows):
    listed = Post.objects.as_user(people["alice"])
    assert listed.values("title").count() == 2
    assert listed.values_list("title").exists()
    with pytest.raises(PermissionDenied):
        listed.values("title").distinct().count()
    with pytest.raises(PermissionDenied):
        listed.values("title").union(listed.values("title")).count()


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_an_update_reads_its_explicit_ordering(people, rows, monkeypatch):
    edited = Post.objects.as_user(people["bob"])
    with pytest.raises(PermissionDenied, match="Cannot filter or order by gated field"):
        edited.order_by("title").update(body="ordered")
    # A model's default ordering is not part of an UPDATE.
    monkeypatch.setattr(Post._meta, "ordering", ["title"])
    assert edited.filter(body="").update(body="default") == 2
