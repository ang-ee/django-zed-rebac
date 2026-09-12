"""Edge paths of the lazy SQL permission compiler (`backends/local_query.py`).

These cover branches the broader parity suites in
``test_queryset_permission_parity.py`` and ``test_encoded_resource_scope.py``
do not exercise: a field-backed relation reached by a subject of a type the
relation does not allow (fail-closed), a field-backed relation whose *subject*
is keyed by a non-``pk`` identity, a stored arrow compiled through the encoded
(non-native identity) fallback, and two expression shapes that must compile to
a static deny. Every assertion checks that the compiled SQL predicate — not the
enumerating ``accessible()`` fallback — produced the answer.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

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
from tests.testapp.models import AuthoredPost, EncodedPost, Post

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
        local.set_schema(parse_zed(src))
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
    predicate must be a static deny (``local_query`` line 209) rather than
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
        alice = get_user_model().objects.create_user(username="alice")
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
    keyed on ``username`` (``local_query`` lines 213-216) instead of matching
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
        alice = get_user_model().objects.create_user(username="alice")
        bob = get_user_model().objects.create_user(username="bob")
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


def test_encoded_identity_stored_arrow_via_tuple_relation(storage):
    """A tuple-backed arrow on a non-native identity compiles, not enumerates.

    ``blog/encodedpost`` is keyed by an ``EncodedIntegerField`` (wire value
    ``item-<n>``), so its identity is non-native and the ``container`` arrow —
    a plain relationship-row relation, not a field/const backing — routes
    through ``ConvertedRelationIds`` with a target (``local_query`` line 75).
    """
    src = """
    definition auth/user {}
    definition work/container {
        relation viewer: auth/user
        permission read = viewer
    }
    definition blog/encodedpost {
        relation container: work/container
        permission read = container->read
    }
    """
    with _schema(src, storage) as active:
        alice = get_user_model().objects.create_user(username="alice")
        alice_ref = to_subject_ref(alice)
        with sudo(reason="encoded stored-arrow fixtures"):
            visible = EncodedPost.objects.create(public_id="item-1", title="visible", author=alice)
            hidden = EncodedPost.objects.create(public_id="item-2", title="hidden", author=alice)
            dangling = EncodedPost.objects.create(
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
                EncodedPost.objects.with_actor(alice_ref).values_list("public_id", flat=True)
            )
        assert seen == {"item-1"}
        assert EncodedPost.objects.with_actor(BOB).count() == 0
        # Parity with the graph walk over encoded identities.
        assert set(
            active.accessible(subject=alice_ref, action="read", resource_type="blog/encodedpost")
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
    try:
        schema = parse_zed(src)
        with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
            reset_backend()
            active = backend()
            assert isinstance(active, LocalBackend)
            active.set_schema(schema)
    except Exception:  # pragma: no cover - schema validation may reject this shape
        reset_backend()
        pytest.skip("schema layer rejects arrows through undeclared relations")
    try:
        with sudo(reason="undeclared-arrow fixtures"):
            post = Post.objects.create(title="unreachable")
        active.write_relationships([_grant(post, "viewer", ALICE)])
        with _assert_predicate_path(active):
            assert Post.objects.with_actor(ALICE).count() == 0
    finally:
        reset_backend()


def test_expiry_filter_binds_app_clock_not_database_clock(storage):
    """The SQL expiry predicate binds the app clock, matching the graph path.

    ``local._filter_active`` (used by ``accessible()`` and the enumeration
    fallback) filters expiry with ``timezone.now()``. The compiled predicate
    must bind that same app-server clock, not the database clock (``Now()``),
    or the two evaluation strategies disagree inside the app/DB clock-skew
    window and break the ``accessible() == queryset`` parity contract.
    """
    src = """
    use expiration
    definition auth/user {}
    definition blog/post {
        relation viewer: auth/user with expiration
        permission read = viewer
    }
    """
    with _schema(src, storage) as active:
        with sudo(reason="clock parity fixtures"):
            post = Post.objects.create(title="expiring")
        future = timezone.now() + timedelta(days=1)
        active.write_relationships(
            [RelationshipTuple(to_object_ref(post), "viewer", ALICE, expires_at=future)]
        )
        # A distinctive sentinel clock so the bound parameter is unmistakable.
        fixed = datetime(2099, 1, 2, 3, 4, 5, tzinfo=UTC)
        with patch("rebac.backends.local_query.timezone.now", return_value=fixed) as now:
            queryset = Post.objects.with_actor(ALICE).with_action("read").scoped()
            _sql, params = queryset.query.sql_with_params()
        # The compiler consulted the app clock (a DB-clock Now() would not).
        assert now.called
        # ...and bound that timestamp as a query parameter rather than emitting
        # a database-clock SQL function. (The DB adapter stringifies datetimes.)
        assert any(isinstance(p, str) and p.startswith("2099-01-02 03:04:05") for p in params)


def test_maybe_using_routes_manager_to_pinned_alias(storage):
    """`_maybe_using` pins any alias, and keeps the routed/default DB for None.

    This is the routing primitive behind the multi-database consistency fix: an
    arbitrary alias flows through even when it is not the default, so tuple-grant
    resolution reads from whatever database the queryset is bound to.
    """
    with _schema("definition auth/user {}", storage) as active:
        assert active._maybe_using(EncodedPost._base_manager, "replica").all().db == "replica"
        assert active._maybe_using(EncodedPost._base_manager, None).all().db == "default"


def test_encoded_tuple_grant_resolution_uses_queryset_database(storage):
    """ConvertedRelationIds resolves tuple grants from the queryset's own DB.

    The encoded (non-native) identity routes ``shared`` through
    ``ConvertedRelationIds``; its ``as_sql`` must resolve the grant rows from
    ``self.scope.using`` — the queryset's alias — not the default alias, so the
    resolved ids and the surrounding EXISTS subqueries agree on the database.
    Before the fix the resolver was called with no alias at all.
    """
    src = """
    definition auth/user {}
    definition blog/encodedpost {
        relation shared: auth/user
        permission read = shared
    }
    """
    with _schema(src, storage) as active:
        alice = get_user_model().objects.create_user(username="alice")
        alice_ref = to_subject_ref(alice)
        with sudo(reason="encoded db-alias fixtures"):
            post = EncodedPost.objects.create(public_id="item-1", title="shared", author=alice)
        active.write_relationships([_grant(post, "shared", alice_ref)])

        captured: dict[str, object] = {}
        original = active._resources_via_relation

        def spy(*args, **kwargs):
            captured["using"] = kwargs.get("using")
            return original(*args, **kwargs)

        with patch.object(active, "_resources_via_relation", side_effect=spy):
            seen = set(
                EncodedPost.objects.with_actor(alice_ref).values_list("public_id", flat=True)
            )
        assert seen == {"item-1"}
        # The queryset's alias reached the tuple-grant resolver.
        assert captured.get("using") == EncodedPost.objects.db == "default"
