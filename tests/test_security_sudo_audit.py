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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RebacQuerySet._bypass installs the queryset bypass without emitting an audit row "
        "(src/rebac/managers.py:279)."
    ),
)
def test_queryset_sudo_writes_bypass_row(post):
    assert list(Post.objects.sudo(reason="audit.queryset-sudo")) == [post]

    assert _bypass_rows("audit.queryset-sudo").exists()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RebacQuerySet.system_context goes through _bypass, which emits no audit row "
        "(src/rebac/managers.py:279)."
    ),
)
def test_queryset_system_context_writes_bypass_row(post):
    assert list(Post.objects.system_context(reason="audit.queryset-system")) == [post]

    assert _bypass_rows("audit.queryset-system").exists()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RebacMixin.sudo only sets _rebac_sudo_reason and emits no audit row "
        "(src/rebac/mixins.py:450)."
    ),
)
def test_instance_sudo_writes_bypass_row(post):
    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance.title = "changed under instance sudo"

    instance.sudo(reason="audit.instance-sudo").save()

    assert _bypass_rows("audit.instance-sudo").exists()


def test_block_sudo_writes_bypass_row_inside_block(post):
    with sudo(reason="audit.block-sudo"):
        assert _bypass_rows("audit.block-sudo").count() == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "sudo() writes its audit row on the caller's connection inside the caller's "
        "transaction, so an outer rollback removes it (src/rebac/actors.py:342)."
    ),
)
@pytest.mark.django_db(transaction=True)
def test_block_sudo_row_survives_outer_rollback():
    with pytest.raises(_Rollback):
        with transaction.atomic():
            with sudo(reason="audit.rolled-back"):
                assert _bypass_rows("audit.rolled-back").count() == 1
            raise _Rollback

    assert _bypass_rows("audit.rolled-back").count() == 1
