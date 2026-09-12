"""Permission predicates must use a field's wire/storage conversion contract."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Count
from django.test import override_settings

from rebac import (
    LocalBackend,
    ObjectRef,
    PermissionResult,
    RelationshipTuple,
    SubjectRef,
    backend,
    evaluator_scope,
    sudo,
    to_object_ref,
    to_subject_ref,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.testapp.models import EncodedFolder, EncodedPost, EncodedPrimaryPost

pytestmark = pytest.mark.django_db(transaction=True)

SCHEMA = """
definition auth/user {}
definition blog/encodedfolder {
    relation owner: auth/user // rebac:field=author
    relation shared: auth/user
    relation blocked: auth/user
    permission read = (owner + shared) - blocked
}
definition blog/encodedpost {
    relation owner: auth/user // rebac:field=author
    relation shared: auth/user
    relation blocked: auth/user
    relation folder: blog/encodedfolder // rebac:field=folder
    permission read = ((owner + shared) + folder->read) - blocked
}
definition blog/encodedprimarypost {
    relation owner: auth/user // rebac:field=author
    relation shared: auth/user
    relation blocked: auth/user
    relation folder: blog/encodedfolder // rebac:field=folder
    permission read = ((owner + shared) + folder->read) - blocked
}
"""


@pytest.fixture(params=["denormalized", "registry"])
def active(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        local = backend()
        assert isinstance(local, LocalBackend)
        local.set_schema(parse_zed(SCHEMA))
        try:
            yield local
        finally:
            reset_backend()


@pytest.fixture(params=[(EncodedPost, "public_id"), (EncodedPrimaryPost, "pk")])
def item_type(request):
    return request.param


@pytest.fixture
def actors(active):
    return (
        get_user_model().objects.create_user(username="alice"),
        get_user_model().objects.create_user(username="bob"),
    )


def _grant(row, relation, actor):
    return RelationshipTuple(to_object_ref(row), relation, to_subject_ref(actor))


def _make_item(item_type, number, title, author, folder=None):
    model, identity = item_type
    return model.objects.create(
        **{identity: f"item-{number}"}, title=title, author=author, folder=folder
    )


def _assert_visible(active, queryset, actor, rows, expected):
    expected_ids = {str(to_object_ref(row).resource_id) for row in expected}
    allowed_by_check = {
        str(to_object_ref(row).resource_id)
        for row in rows
        if active.has_access(
            subject=to_subject_ref(actor), action="read", resource=to_object_ref(row)
        )
    }
    assert allowed_by_check == expected_ids
    materialized = list(queryset)
    assert {to_object_ref(row).resource_id for row in materialized} == expected_ids
    assert len(materialized) == len(expected_ids)
    assert queryset.all().count() == len(expected_ids)
    assert queryset.all().aggregate(count=Count("pk")) == {"count": len(expected_ids)}
    projected = queryset.scoped_for_aggregate().values("pk")
    assert set(
        queryset.model._base_manager.filter(pk__in=projected).values_list("pk", flat=True)
    ) == {row.pk for row in expected}


def test_encoded_identity_ownership_shares_and_exclusions_agree_with_check_access(
    active, item_type, actors
):
    alice, bob = actors
    with sudo(reason="encoded identity visibility fixtures"):
        owned = _make_item(item_type, 101, "owned", alice)
        shared = _make_item(item_type, 102, "shared", bob)
        blocked = _make_item(item_type, 103, "blocked", alice)
        private = _make_item(item_type, 104, "private", bob)
    active.write_relationships([_grant(shared, "shared", alice), _grant(blocked, "blocked", alice)])
    rows = [owned, shared, blocked, private]
    model, identity = item_type
    assert list(
        model.objects.sudo(reason="verify encoded field conversion")
        .filter(**{identity: "item-102"})
        .values_list(identity, flat=True)
    ) == ["item-102"]
    _assert_visible(active, model.objects.with_actor(alice), alice, rows, [owned, shared])
    _assert_visible(active, model.objects.with_actor(bob), bob, rows, [shared, private])


def test_encoded_identity_eager_scope_observes_grant_and_revoke(active, item_type, actors):
    alice, bob = actors
    model, _identity = item_type
    with sudo(reason="encoded identity revocation fixtures"):
        post = _make_item(item_type, 201, "shared", bob)
    shared = _grant(post, "shared", alice)
    blocked = _grant(post, "blocked", alice)
    active.write_relationships([shared])
    with evaluator_scope():
        eager = model.objects.with_actor(alice).scoped()
        _assert_visible(active, model.objects.with_actor(alice), alice, [post], [post])
        active.delete_relationship(shared)
        _assert_visible(active, eager, alice, [post], [])
        before_grant = model.objects.with_actor(alice).scoped_for_aggregate()
        active.write_relationships([shared])
        _assert_visible(active, before_grant, alice, [post], [post])
        before_exclusion = model.objects.with_actor(alice).scoped()
        active.write_relationships([blocked])
        _assert_visible(active, before_exclusion, alice, [post], [])
        before_unblock = model.objects.with_actor(alice).scoped()
        active.delete_relationship(blocked)
        _assert_visible(active, before_unblock, alice, [post], [post])


def test_field_arrow_into_encoded_identity_keeps_shared_and_owned_targets(
    active, item_type, actors
):
    alice, bob = actors
    model, _identity = item_type
    with sudo(reason="encoded identity field arrow fixtures"):
        owned_folder = EncodedFolder.objects.create(
            public_id="item-501", name="owned", author=alice
        )
        shared_folder = EncodedFolder.objects.create(
            public_id="item-502", name="shared", author=bob
        )
        blocked_folder = EncodedFolder.objects.create(
            public_id="item-503", name="blocked", author=alice
        )
        owned = _make_item(item_type, 301, "inherited owner", bob, owned_folder)
        shared = _make_item(item_type, 302, "inherited share", bob, shared_folder)
        blocked = _make_item(item_type, 303, "inherited block", bob, blocked_folder)
        private = _make_item(item_type, 304, "private", bob)
    share = _grant(shared_folder, "shared", alice)
    active.write_relationships([share, _grant(blocked_folder, "blocked", alice)])
    rows = [owned, shared, blocked, private]
    eager = model.objects.with_actor(alice).scoped()
    _assert_visible(active, model.objects.with_actor(alice), alice, rows, [owned, shared])
    active.delete_relationship(share)
    _assert_visible(active, eager, alice, rows, [owned])


def test_encoded_owner_corpus_is_not_enumerated_for_sparse_tuple_grants(active, item_type, actors):
    alice, bob = actors
    model, identity = item_type
    with sudo(reason="encoded identity performance fixtures"):
        _make_item(item_type, 100, "owned", alice)
        shared = _make_item(item_type, 101, "shared", bob)
        blocked = _make_item(item_type, 102, "blocked", alice)
        _make_item(item_type, 103, "private", bob)
    active.write_relationships([_grant(shared, "shared", alice), _grant(blocked, "blocked", alice)])

    def measure(expected):
        queries = []

        def record(execute, sql, params, many, context):
            queries.append((sql, len(params or ())))
            return execute(sql, params, many, context)

        with evaluator_scope():
            model.objects.with_actor(alice).count()
            with connection.execute_wrapper(record):
                queryset = model.objects.with_actor(alice).scoped_for_aggregate()
                assert queryset.count() == expected
                assert len(list(queryset.order_by("pk").values_list("pk", flat=True)[:25])) == min(
                    expected, 25
                )
        return len(queries), max(parameters for _sql, parameters in queries)

    with patch.object(
        active, "accessible", side_effect=AssertionError("enumerated root ownership")
    ):
        small_cost = measure(2)
        with sudo(reason="grow encoded owner corpus"):
            model.objects.bulk_create(
                [
                    model(**{identity: f"item-{1000 + index}"}, title="owned", author=alice)
                    for index in range(2000)
                ]
                + [
                    model(**{identity: f"item-{5000 + index}"}, title="private", author=bob)
                    for index in range(2000)
                ]
            )
        large_cost = measure(2002)
    assert large_cost == small_cost
    assert large_cost[0] <= 16
    assert large_cost[1] < 100


def test_encoded_exclusion_with_caveated_group_membership_falls_back_wholly(
    active, item_type, actors
):
    alice, _bob = actors
    model, _identity = item_type
    schema = SCHEMA.replace(
        "definition auth/user {}",
        """
        caveat admitted(allowed bool) { allowed }
        definition auth/user {}
        definition auth/group { relation member: auth/user with admitted }
        """,
    ).replace("relation blocked: auth/user", "relation blocked: auth/group#member")
    active.set_schema(parse_zed(schema))
    with sudo(reason="encoded conditional exclusion fixtures"):
        visible = _make_item(item_type, 701, "visible", alice)
        conditional = _make_item(item_type, 702, "conditional", alice)
    active.write_relationships(
        [
            RelationshipTuple(
                ObjectRef("auth/group", "blocked"),
                "member",
                to_subject_ref(alice),
                caveat_name="admitted",
            ),
            _grant(conditional, "blocked", SubjectRef.of("auth/group", "blocked", "member")),
        ]
    )
    assert (
        active.check_access(
            subject=to_subject_ref(alice), action="read", resource=to_object_ref(conditional)
        ).result
        == PermissionResult.CONDITIONAL_PERMISSION
    )
    with patch.object(active, "accessible", wraps=active.accessible) as enumerate_resources:
        assert list(model.objects.with_actor(alice).values_list("pk", flat=True)) == [visible.pk]
    enumerate_resources.assert_called_once()
    assert enumerate_resources.call_args.kwargs["action"] == "read"
    _assert_visible(
        active, model.objects.with_actor(alice), alice, [visible, conditional], [visible]
    )
