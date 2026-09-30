"""Fail-closed scopes and canonical subject identities on the index read path."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from rebac import (
    LocalBackend,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    to_object_ref,
    to_subject_ref,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import atomic_source_write, install_schema
from tests.testapp.models import AuthoredPost, Post, TextIdentityPost

pytestmark = pytest.mark.django_db(transaction=True)

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")


@contextmanager
def _schema(src, storage):
    """Activate ``src`` on a fresh backend under one storage strategy."""
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        reset_backend()
        local = backend()
        assert isinstance(local, LocalBackend)
        install_schema(local, parse_zed(src))
        try:
            yield local
        finally:
            reset_backend()


def _grant(row, relation, subject):
    return RelationshipTuple(to_object_ref(row), relation, subject)


@contextmanager
def _assert_predicate_path(active):
    """Fail if the compiler fell back to the enumerating evaluator."""
    with patch.object(
        active, "accessible", side_effect=AssertionError("fell back to accessible()")
    ):
        yield


@pytest.fixture(params=["denormalized", "registry"])
def storage(request):
    return request.param


def test_field_backed_permission_denies_subject_of_disallowed_type(storage):
    """A field-backed relation only admits its declared subject type.

    ``owner`` is declared ``auth/user`` and backed by the ``author`` FK. A
    group subject-set actor matches no allowed subject, so the compiled
    predicate must deny rather than
    leaking every row.
    """
    src = """
    definition auth/user {}
    definition auth/group { relation member: auth/user }
    definition blog/authoredpost {
        relation owner: auth/user // rebac:field=author
        permission read = owner
    }
    """
    with _schema(src, storage) as active:
        alice = atomic_source_write(get_user_model().objects.create_user, username="alice")
        with sudo(reason="disallowed-subject fixtures"):
            AuthoredPost.objects.create(title="owned", author=alice)
            AuthoredPost.objects.create(title="other", author=alice)
        group = SubjectRef.of("auth/group", "editors", "member")
        with _assert_predicate_path(active):
            assert AuthoredPost.objects.with_actor(group).count() == 0
            assert not AuthoredPost.objects.with_actor(group).exists()
        # Parity: the graph walk agrees the group sees nothing.
        assert (
            list(active.accessible(subject=group, action="read", resource_type="blog/authoredpost"))
            == []
        )
        # The owner still sees their rows through the same predicate.
        with _assert_predicate_path(active):
            assert AuthoredPost.objects.with_actor(to_subject_ref(alice)).count() == 2


def test_field_backed_owner_resolves_non_pk_subject_identity(storage):
    """Field-backed owners honor a non-``pk`` subject identity.

    With ``REBAC_USER_ID_ATTR = "username"`` the actor is
    ``auth/user:<username>`` while the ``author`` FK still stores the row's
    integer pk. The compiler must correlate through a destination subquery
    keyed on ``username`` instead of matching
    the raw subject id against ``author_id``.
    """
    src = """
    definition auth/user {}
    definition blog/authoredpost {
        relation owner: auth/user // rebac:field=author
        permission read = owner
    }
    """
    with override_settings(REBAC_USER_ID_ATTR="username"), _schema(src, storage) as active:
        alice = atomic_source_write(get_user_model().objects.create_user, username="alice")
        bob = atomic_source_write(get_user_model().objects.create_user, username="bob")
        alice_ref = to_subject_ref(alice)
        # The actor id is the username, not the pk.
        assert alice_ref == SubjectRef.of("auth/user", "alice")
        with sudo(reason="non-pk subject identity fixtures"):
            owned = AuthoredPost.objects.create(title="owned", author=alice)
            other = AuthoredPost.objects.create(title="other", author=bob)
        with _assert_predicate_path(active):
            visible = set(AuthoredPost.objects.with_actor(alice_ref).values_list("pk", flat=True))
        assert visible == {owned.pk}
        assert other.pk not in visible
        # Matching the username against author_id (the un-translated path) would
        # have mismatched types and returned nothing for everyone.
        with _assert_predicate_path(active):
            assert AuthoredPost.objects.with_actor(to_subject_ref(bob)).count() == 1


def test_text_identity_stored_arrow_via_tuple_relation(storage):
    """Tuple-backed arrows retain visibility, dangling-target and identity parity."""
    src = """
    definition auth/user {}
    definition work/container {
        relation viewer: auth/user
        permission read = viewer
    }
    definition blog/textidentitypost {
        relation container: work/container
        permission read = container->read
    }
    """
    with _schema(src, storage) as active:
        alice = atomic_source_write(get_user_model().objects.create_user, username="alice")
        alice_ref = to_subject_ref(alice)
        with sudo(reason="encoded stored-arrow fixtures"):
            visible = TextIdentityPost.objects.create(
                public_id="item-1", title="visible", author=alice
            )
            hidden = TextIdentityPost.objects.create(
                public_id="item-2", title="hidden", author=alice
            )
            dangling = TextIdentityPost.objects.create(
                public_id="item-3", title="dangling", author=alice
            )
        container = SubjectRef.of("work/container", "c1")
        other = SubjectRef.of("work/container", "c2")
        active.write_relationships(
            [
                _grant(visible, "container", container),
                _grant(hidden, "container", other),
                _grant(dangling, "container", SubjectRef.of("work/container", "missing")),
                RelationshipTuple(container.object, "viewer", alice_ref),
            ]
        )
        with _assert_predicate_path(active):
            seen = set(
                TextIdentityPost.objects.with_actor(alice_ref).values_list("public_id", flat=True)
            )
        assert seen == {"item-1"}
        assert TextIdentityPost.objects.with_actor(BOB).count() == 0
        # Parity with the graph walk over encoded identities.
        assert set(
            active.accessible(
                subject=alice_ref, action="read", resource_type="blog/textidentitypost"
            )
        ) == {"item-1"}


def test_nil_permission_compiles_to_static_deny(storage):
    """``permission x = nil`` compiles to a deny, returning no rows."""
    src = """
    definition auth/user {}
    definition blog/post {
        relation viewer: auth/user
        permission read = nil
    }
    """
    with _schema(src, storage) as active:
        with sudo(reason="nil permission fixtures"):
            post = Post.objects.create(title="unreachable")
        active.write_relationships([_grant(post, "viewer", ALICE)])
        with _assert_predicate_path(active):
            assert Post.objects.with_actor(ALICE).count() == 0
        assert (
            list(active.accessible(subject=ALICE, action="read", resource_type="blog/post")) == []
        )


def test_arrow_via_undeclared_relation_compiles_to_static_deny(storage):
    """An arrow whose ``via`` names no relation denies rather than raising."""
    src = """
    definition auth/user {}
    definition blog/post {
        relation viewer: auth/user
        permission read = ghost->read
    }
    """
    schema = parse_zed(src)
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        reset_backend()
        active = backend()
        assert isinstance(active, LocalBackend)
        install_schema(active, schema)
    try:
        with sudo(reason="undeclared-arrow fixtures"):
            post = Post.objects.create(title="unreachable")
        active.write_relationships([_grant(post, "viewer", ALICE)])
        with _assert_predicate_path(active):
            assert Post.objects.with_actor(ALICE).count() == 0
    finally:
        reset_backend()


def test_scope_binds_application_clock_not_database_clock(storage, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from rebac.index import time as index_time

    src = """
    use expiration
    definition auth/user {}
    definition blog/post {
        relation viewer: auth/user with expiration
        permission read = viewer
    }
    """
    # The database clock stays near the real present throughout the test.
    instant = timezone.now() + timedelta(days=100)
    deadline = instant + timedelta(hours=1)
    monkeypatch.setattr(index_time, "index_now", lambda: instant)
    with _schema(src, storage) as active:
        with sudo(reason="application clock regression"):
            post = Post.objects.create(title="expires by the app clock")
        active.write_relationships(
            [RelationshipTuple(to_object_ref(post), "viewer", ALICE, expires_at=deadline)]
        )
        scoped = Post.objects.with_actor(ALICE).scoped()
        assert list(scoped.values_list("pk", flat=True)) == [post.pk]
        instant = deadline
        assert timezone.now() < deadline
        assert list(scoped.values_list("pk", flat=True)) == []
        sql, params = scoped.query.get_compiler(using="default").as_sql()
        assert "CURRENT_TIMESTAMP" not in sql.upper()
        assert "STATEMENT_TIMESTAMP" not in sql.upper()
        assert any(str(deadline.date()) in str(value) for value in params)
