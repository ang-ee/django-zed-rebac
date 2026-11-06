"""Every sudo bypass writes a ``sudo.bypass`` audit row, and the row persists."""

from __future__ import annotations

import pytest
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Subquery
from django.test.utils import CaptureQueriesContext

from rebac import SubjectRef, actor_context, backend, sudo
from rebac.backends import reset_backend
from rebac.models import PermissionAuditEvent
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.testapp.models import Post

SCHEMA_TEXT = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    permission read = owner
    permission write = owner
    permission delete = owner
    permission create = owner
}
"""


class _Rollback(Exception):
    pass


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


@pytest.fixture
def post(db):
    with sudo(reason="test.fixture"):
        return Post.objects.create(title="hello")


def _bypass_rows(reason):
    return PermissionAuditEvent.objects.filter(
        kind=PermissionAuditEvent.KIND_SUDO_BYPASS, reason=reason
    )


def _audit_inserts(queries):
    return sum(
        'INSERT INTO "rebac_permissionauditevent"' in query["sql"]
        for query in queries.captured_queries
    )


def test_queryset_sudo_writes_bypass_row(post):
    assert list(Post.objects.sudo(reason="audit.queryset-sudo")) == [post]

    assert _bypass_rows("audit.queryset-sudo").count() == 1


def test_queryset_sudo_audits_only_when_used_once(post):
    rows = Post.objects.sudo(reason="audit.lazy")
    assert not _bypass_rows("audit.lazy").exists()
    assert rows.count() == 1
    assert list(rows) == [post]
    assert _bypass_rows("audit.lazy").count() == 1


def test_queryset_sudo_raw_is_audited(post):
    rows = list(Post.objects.sudo(reason="audit.raw").raw(f"SELECT * FROM {Post._meta.db_table}"))
    assert [row.pk for row in rows] == [post.pk]
    assert _bypass_rows("audit.raw").count() == 1


def test_scoped_under_block_sudo_does_not_audit_twice(post):
    with sudo(reason="audit.scoped-block"):
        assert list(Post.objects.scoped()) == [post]
    assert _bypass_rows("audit.scoped-block").count() == 1


def test_queryset_system_context_writes_bypass_row(post):
    assert list(Post.objects.system_context(reason="audit.queryset-system")) == [post]

    assert _bypass_rows("audit.queryset-system").count() == 1


def test_instance_sudo_writes_bypass_row(post):
    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.title = "changed under instance sudo"

    instance.sudo(reason="audit.instance-sudo").save()

    assert _bypass_rows("audit.instance-sudo").count() == 1


def test_block_sudo_writes_bypass_row_inside_block(post):
    with sudo(reason="audit.block-sudo"):
        assert _bypass_rows("audit.block-sudo").count() == 1


@pytest.mark.django_db(transaction=True)
def test_block_sudo_row_follows_outer_transaction():
    with pytest.raises(_Rollback):
        with transaction.atomic():
            with sudo(reason="audit.rolled-back"):
                assert _bypass_rows("audit.rolled-back").count() == 1
            raise _Rollback

    assert _bypass_rows("audit.rolled-back").count() == 0
    with transaction.atomic():
        with sudo(reason="audit.committed"):
            assert _bypass_rows("audit.committed").count() == 1
    assert _bypass_rows("audit.committed").count() == 1


def test_building_a_sudo_subquery_writes_no_audit_row(post):
    """Building or compiling an expression does not evaluate its bypass."""

    expression = ~Exists(Post.objects.sudo(reason="audit.embedded").filter(pk=OuterRef("pk")))
    queryset = Post.objects.sudo(reason="audit.outer").annotate(flag=expression)
    assert not _bypass_rows("audit.embedded").exists()
    assert not _bypass_rows("audit.outer").exists()
    str(queryset.query)
    assert not _bypass_rows("audit.embedded").exists()

    assert list(queryset.values("pk", "flag")) == [{"pk": post.pk, "flag": False}]
    assert _bypass_rows("audit.embedded").count() == 1
    assert list(queryset.values_list("flag", flat=True)) == [False]
    assert _bypass_rows("audit.embedded").count() == 2


@pytest.mark.pg_delta
@pytest.mark.parametrize("uses", [1, 3, 7])
def test_one_embedded_bypass_entry_has_constant_audit_statement_cost(post, uses):
    reason = f"audit.reused.{uses}"
    inner = Post.objects.system_context(reason=reason)
    outer = Post.objects.system_context(reason=f"audit.outer.{uses}")
    for index in range(uses):
        if index % 3 == 0:
            outer = outer.filter(pk__in=inner.values("pk"))
        elif index % 3 == 1:
            outer = outer.annotate(
                **{f"selected_{index}": Subquery(inner.filter(pk=OuterRef("pk")).values("pk")[:1])}
            )
        else:
            outer = outer.annotate(**{f"matched_{index}": Exists(inner.filter(pk=OuterRef("pk")))})
    projection = ("pk", *outer.query.annotations)
    str(outer.query)
    assert not _bypass_rows(reason).exists()

    with CaptureQueriesContext(connection) as queries:
        assert list(outer.values_list(*projection))

    assert _bypass_rows(reason).count() == 1
    # The number of INSERT statements is constant as occurrences grow:
    # one for this entry, one for the outer queryset's own bypass.
    assert _audit_inserts(queries) == 2


@pytest.mark.pg_delta
def test_embedded_bypass_audits_each_execution_but_no_compilation(post):
    reason = "audit.count-then-list"
    inner = Post.objects.system_context(reason=reason).values("pk")
    outer = Post.objects.system_context(reason="audit.count-then-list.outer").filter(pk__in=inner)
    str(outer.query)
    str(outer.query)
    assert not _bypass_rows(reason).exists()

    with CaptureQueriesContext(connection) as counted:
        assert outer.count() == 1
    assert _bypass_rows(reason).count() == 1
    with CaptureQueriesContext(connection) as listed:
        assert list(outer) == [post]
    assert _bypass_rows(reason).count() == 2
    assert _audit_inserts(counted) == 2
    assert _audit_inserts(listed) == 1


@pytest.mark.pg_delta
@pytest.mark.parametrize("entries", [1, 2, 4])
def test_nested_bypasses_audit_linearly_in_entries(post, entries):
    reason = f"audit.nested.{entries}"
    nested = Post.objects.system_context(reason=reason).values("pk")
    for _ in range(entries - 1):
        nested = Post.objects.system_context(reason=reason).filter(pk__in=nested).values("pk")
    outer = Post.objects.system_context(reason="audit.nested.outer").filter(pk__in=nested)
    str(outer.query)
    assert not _bypass_rows(reason).exists()

    with CaptureQueriesContext(connection) as queries:
        assert list(outer) == [post]

    assert _bypass_rows(reason).count() == entries
    assert _audit_inserts(queries) == entries + 1


@pytest.mark.pg_delta
def test_embedded_bypass_in_plain_queryset_audits_on_execution(post):
    reason = "audit.plain-outer"
    inner = Post.objects.system_context(reason=reason).values("pk")
    outer = Post._base_manager.filter(pk__in=inner)
    str(outer.query)
    assert not _bypass_rows(reason).exists()

    actor = SubjectRef.of("auth/user", "audit-reader")
    with actor_context(actor):
        assert list(outer) == [post]
    assert _bypass_rows(reason).count() == 1
    row = _bypass_rows(reason).get()
    assert (row.actor_subject_type, row.actor_subject_id) == (
        actor.subject_type,
        actor.subject_id,
    )


@pytest.mark.pg_delta
@pytest.mark.parametrize("plain_outer", [False, True])
def test_sliced_count_executes_embedded_bypass_once(post, plain_outer):
    reason = f"audit.sliced-count.{plain_outer}"
    inner = Post.objects.system_context(reason=reason).values("pk")
    root = Post._base_manager if plain_outer else Post.objects.system_context(reason="outer")
    outer = root.filter(pk__in=inner)[:1]
    str(outer.query)
    assert not _bypass_rows(reason).exists()

    assert outer.count() == 1
    assert _bypass_rows(reason).count() == 1


@pytest.mark.pg_delta
@pytest.mark.parametrize("uses", [1, 3, 7])
def test_write_statement_audits_embedded_bypass_once_across_occurrences(post, uses):
    reason = f"audit.write.{uses}"
    inner = Post.objects.system_context(reason=reason).values("pk")
    outer = Post.objects.system_context(reason=f"audit.write.outer.{uses}")
    for _ in range(uses):
        outer = outer.filter(pk__in=inner)

    with CaptureQueriesContext(connection) as queries:
        assert outer.update(title="changed") == 1

    assert _bypass_rows(reason).count() == 1
    assert _audit_inserts(queries) == 2


@pytest.mark.pg_delta
def test_plain_exists_over_plain_middle_audits_embedded_bypass(post):
    inner = Post.objects.system_context(reason="audit.plain-exists").values("pk")
    middle = Post._base_manager.filter(pk__in=inner, pk=OuterRef("pk"))
    outer = Post._base_manager.annotate(found=Exists(middle))

    assert [row.found for row in outer] == [True]
    assert _bypass_rows("audit.plain-exists").count() == 1


@pytest.mark.pg_delta
def test_plain_outer_over_actor_scoped_middle_audits_bypass(post):
    actor = SubjectRef.of("auth/user", "no-posts")
    inner = Post.objects.system_context(reason="audit.actor-middle").values("pk")
    middle = Post.objects.with_actor(actor).filter(pk__in=inner).values("pk")
    outer = Post._base_manager.filter(pk__in=middle)

    assert list(outer) == []
    assert _bypass_rows("audit.actor-middle").count() == 1


@pytest.mark.pg_delta
def test_cte_mixed_plain_outer_audits_embedded_bypass(post):
    from django_cte import CTE, with_cte

    cte = CTE(Post._base_manager.values("pk"), name="audit_probe_cte")
    outer = with_cte(cte, select=Post._base_manager.all())
    inner = Post.objects.system_context(reason="audit.cte-outer").values("pk")

    assert list(outer.filter(pk__in=inner)) == [post]
    assert _bypass_rows("audit.cte-outer").count() == 1


@pytest.mark.pg_delta
def test_update_set_subquery_audits_embedded_bypass(post):
    inner = (
        Post.objects.system_context(reason="audit.update-value")
        .filter(pk=OuterRef("pk"))
        .values("title")[:1]
    )

    assert Post._base_manager.filter(pk=post.pk).update(body=Subquery(inner)) == 1
    assert Post._base_manager.get(pk=post.pk).body == "hello"
    assert _bypass_rows("audit.update-value").count() == 1


@pytest.mark.pg_delta
def test_insert_subquery_audits_embedded_bypass(post):
    inner = Post.objects.system_context(reason="audit.insert-value").values("title")[:1]

    with sudo(reason="audit.insert-block"):
        created = Post.objects.create(title=Subquery(inner))

    assert Post._base_manager.get(pk=created.pk).title == "hello"
    assert _bypass_rows("audit.insert-value").count() == 1


@pytest.mark.pg_delta
def test_collector_fast_delete_audits_embedded_bypass(post):
    inner = Post.objects.system_context(reason="audit.fast-delete").values("pk")

    assert PermissionAuditEvent.objects.filter(pk__in=inner, reason="absent").delete()[0] == 0
    assert _bypass_rows("audit.fast-delete").count() == 1


@pytest.mark.pg_delta
def test_two_ambient_captures_keep_both_reasons(post):
    base = Post.objects.all()
    with sudo(reason="audit.capture-a"):
        a = base.scoped()
    with sudo(reason="audit.capture-b"):
        b = base.scoped()
    assert _bypass_rows("audit.capture-a").count() == 1
    assert _bypass_rows("audit.capture-b").count() == 1
    outer = (
        Post.objects.system_context(reason="audit.capture-outer")
        .filter(pk__in=a.values("pk"))
        .filter(pk__in=b.values("pk"))
    )

    assert list(outer) == [post]
    assert _bypass_rows("audit.capture-a").count() == 2
    assert _bypass_rows("audit.capture-b").count() == 2


@pytest.mark.pg_delta
@pytest.mark.parametrize("eager", [False, True])
def test_direct_then_embedded_bypass_audits_each_execution(post, eager):
    reason = f"audit.direct-then-embedded.{eager}"
    inner = Post.objects.system_context(reason=reason)
    if eager:
        inner = inner.scoped()
    else:
        assert list(inner) == [post]
    assert _bypass_rows(reason).count() == 1
    outer = Post.objects.system_context(reason=f"audit.outer.{eager}").filter(
        pk__in=inner.values("pk")
    )

    assert list(outer) == [post]
    assert outer.all().count() == 1
    assert _bypass_rows(reason).count() == 3


@pytest.mark.pg_delta
def test_empty_result_set_does_not_audit_embedded_bypass(post):
    inner = Post.objects.system_context(reason="audit.empty-query").values("pk")
    outer = (
        Post.objects.system_context(reason="audit.empty-outer")
        .filter(pk__in=inner)
        .filter(pk__in=[])
    )

    with CaptureQueriesContext(connection) as queries:
        assert list(outer) == []

    assert _bypass_rows("audit.empty-query").count() == 0
    assert not any(
        query["sql"].startswith("SELECT") and "rebac_permissionauditevent" not in query["sql"]
        for query in queries.captured_queries
    )


def test_sudo_queryset_in_a_lookup_is_audited_at_execution(post):
    inner = Post.objects.sudo(reason="audit.lookup").values("pk")
    outer = Post.objects.sudo(reason="audit.outer").filter(pk__in=inner)
    assert not _bypass_rows("audit.lookup").exists()
    assert not _bypass_rows("audit.outer").exists()

    assert [row.pk for row in outer] == [post.pk]
    assert _bypass_rows("audit.lookup").count() == 1


def test_a_bypass_resolved_while_compiling_audits_with_execution_only(post):
    from django.db.models import OuterRef, Subquery

    reason = "audit.ordering"
    inner = Post.objects.system_context(reason=reason).filter(pk=OuterRef("pk")).values("title")
    ordered = Post._base_manager.order_by(Subquery(inner[:1]))
    str(ordered.query)
    str(ordered.query)
    assert not _bypass_rows(reason).exists()
    assert list(ordered) == [post]
    assert _bypass_rows(reason).count() == 1


def test_an_update_pre_select_audits_its_bypass_when_the_update_finds_nothing(db):
    from django.db import models

    from tests.testapp.models import NativeParentLinkedChild

    reason = "audit.pre-select"
    nothing = Post.objects.system_context(reason=reason).filter(title="absent").values("pk")
    rows = models.QuerySet(model=NativeParentLinkedChild).filter(pk__in=nothing)
    # The parent's column makes Django pre-select the keys in a statement of
    # its own; it executes the bypass, though the UPDATE then has no row.
    assert rows.update(name="renamed") == 0
    assert _bypass_rows(reason).count() == 1
