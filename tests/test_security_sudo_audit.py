"""Every sudo bypass writes a ``sudo.bypass`` audit row, and the row persists."""

from __future__ import annotations

import pytest
from django.db import transaction

from rebac import backend, sudo
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
    """Annotations are built at import time; building one runs no query and
    must not write. The embedded bypass is audited each time the statement it
    was resolved into executes."""
    from django.db.models import Exists, OuterRef

    expression = ~Exists(Post.objects.sudo(reason="audit.embedded").filter(pk=OuterRef("pk")))
    queryset = Post.objects.sudo(reason="audit.outer").annotate(flag=expression)
    assert not _bypass_rows("audit.embedded").exists()
    assert not _bypass_rows("audit.outer").exists()

    assert list(queryset.values("pk", "flag")) == [{"pk": post.pk, "flag": False}]
    assert _bypass_rows("audit.embedded").count() == 1
    assert list(queryset.values_list("flag", flat=True)) == [False]
    assert _bypass_rows("audit.embedded").count() == 2


def test_sudo_queryset_in_a_lookup_is_audited_at_execution(post):
    inner = Post.objects.sudo(reason="audit.lookup").values("pk")
    outer = Post.objects.sudo(reason="audit.outer").filter(pk__in=inner)
    assert not _bypass_rows("audit.lookup").exists()
    assert not _bypass_rows("audit.outer").exists()

    assert [row.pk for row in outer] == [post.pk]
    assert _bypass_rows("audit.lookup").count() == 1
