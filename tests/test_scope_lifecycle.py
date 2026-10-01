"""Index scoping stays live across embedding, deadlines and transaction boundaries."""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Subquery
from django.test import override_settings
from django.utils import timezone

from rebac import (
    MissingActorError,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    backend,
    evaluator_scope,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import STORAGE_TIERS, install_schema
from tests.test_recursive_queryscope import (
    ACTOR,
    OUTSIDER,
    chain,
    grant,
    schema_context,
)
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["denormalized", "registry"])
def fixture(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        call_command("rebac", "sync", stdout=StringIO())
        active = backend()
        with sudo(reason="scope lifecycle regression"):
            folder = Folder.objects.create(name="visible")
            visible = Post.objects.create(title="visible", folder=folder)
            hidden = Post.objects.create(title="hidden", folder=folder)
        grant(active, folder, "owner")
        grant(active, visible, "owner")
        yield active, folder, visible, hidden
        reset_backend()


@pytest.mark.parametrize("change", ["relationship", "sync", "rollback", "invalidate", "boundary"])
def test_existing_invalidation_owner_rebuilds(fixture, change):
    active, _, visible, hidden = fixture
    with evaluator_scope() as evaluator:
        assert Post.objects.with_actor(ACTOR).count() == 1

        if change == "relationship":
            grant(active, hidden, "owner")
        elif change == "sync":
            call_command("rebac", "sync", stdout=StringIO())
        elif change == "rollback":
            with transaction.atomic():
                grant(active, hidden, "owner")
                assert Post.objects.with_actor(ACTOR).count() == 2
                transaction.set_rollback(True)
        elif change == "boundary":
            with transaction.atomic():
                assert Post.objects.with_actor(ACTOR).count() == 1
        else:
            evaluator.invalidate()
        expected = 2 if change == "relationship" else 1
        assert Post.objects.with_actor(ACTOR).count() == expected

        assert Post.objects.with_actor(ACTOR).filter(pk=visible.pk).exists()


@pytest.mark.parametrize("embedding", ["subquery", "exists", "in", "prefetch"])
@pytest.mark.parametrize("actor", [ACTOR, OUTSIDER, None])
def test_implicit_expression_scope(fixture, embedding, actor):
    _, folder, visible, _ = fixture
    posts = Post.objects.all()
    if actor is not None:
        posts = posts.with_actor(actor)

    def evaluate():
        parents = Folder._base_manager.filter(pk=folder.pk)
        if embedding == "subquery":
            return list(
                parents.annotate(value=Subquery(posts.order_by("pk").values("pk")[:1])).values_list(
                    "value", flat=True
                )
            )
        if embedding == "exists":
            return list(
                parents.annotate(value=Exists(posts.filter(folder_id=OuterRef("pk")))).values_list(
                    "value", flat=True
                )
            )
        if embedding == "in":
            return list(
                Post._base_manager.filter(pk__in=posts.values("pk")).values_list("pk", flat=True)
            )
        parent = parents.prefetch_related(
            Prefetch("posts", queryset=posts, to_attr="allowed")
        ).get()
        return [row.pk for row in parent.allowed]

    with evaluator_scope():
        if actor is None:
            with pytest.raises(MissingActorError):
                evaluate()
        else:
            expected = {
                "subquery": [visible.pk] if actor == ACTOR else [None],
                "exists": [actor == ACTOR],
                "in": [visible.pk] if actor == ACTOR else [],
                "prefetch": [visible.pk] if actor == ACTOR else [],
            }
            assert evaluate() == expected[embedding]


@pytest.mark.parametrize("storage", STORAGE_TIERS)
@pytest.mark.parametrize("backing", ["tuple", "field"])
@pytest.mark.parametrize("depth", [3, pytest.param(7, marks=pytest.mark.slow)])
def test_recursive_enumeration_and_bulk_guard(storage, backing, depth):
    with schema_context(storage, "folder", backing) as (active, member, hop, action):
        rows = chain(active, hop, backing, depth)
        grant(active, rows[0], member)
        assert set(
            active.accessible(subject=ACTOR, action=action, resource_type="blog/folder")
        ) == {str(row.pk) for row in rows}
        with sudo(reason="overflow candidate"):
            overflow = Folder.objects.create(
                name="overflow", parent=rows[-1] if backing == "field" else None
            )
        if backing == "tuple":
            active.write_relationships(
                [
                    RelationshipTuple(
                        to_object_ref(overflow), hop, SubjectRef.of("blog/folder", str(rows[-1].pk))
                    )
                ]
            )
        # The write guard uses the same recursive permission.
        schema = active.schema()
        from dataclasses import replace

        from rebac.schema.ast import Permission

        definition = schema.get_definition("blog/folder")
        install_schema(
            active,
            replace(
                schema,
                definitions=tuple(
                    replace(
                        d,
                        permissions=(
                            *d.permissions,
                            Permission("write", definition.permissions[0].expression),
                        ),
                    )
                    if d is definition
                    else d
                    for d in schema.definitions
                ),
            ),
        )
        assert set(
            active.accessible(subject=ACTOR, action=action, resource_type="blog/folder")
        ) == {str(row.pk) for row in [*rows, overflow]}
        assert (
            list(active.accessible(subject=OUTSIDER, action=action, resource_type="blog/folder"))
            == []
        )
        with pytest.raises(PermissionDenied, match="Bulk operations are all-or-nothing"):
            Folder.objects.with_actor(OUTSIDER).filter(pk=overflow.pk).update(name="denied")
        assert Folder._base_manager.get(pk=overflow.pk).name == "overflow"
        assert Folder.objects.with_actor(ACTOR).filter(pk=overflow.pk).update(name="allowed") == 1
        assert Folder._base_manager.get(pk=overflow.pk).name == "allowed"


def test_expiration_is_bound_at_execution_even_on_a_reused_scope(fixture):
    active, _, visible, _ = fixture
    install_schema(
        active,
        parse_zed("""
        use expiration
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user with expiration
            permission read = viewer
        }
    """),
    )
    now = timezone.now()
    active.write_relationships(
        [
            RelationshipTuple(
                to_object_ref(visible), "viewer", ACTOR, expires_at=now + timedelta(seconds=1)
            )
        ]
    )
    with evaluator_scope():
        pending = Post.objects.with_actor(ACTOR).scoped()
        with patch("django.utils.timezone.now", return_value=now):
            assert pending.exists()
        with patch("django.utils.timezone.now", return_value=now + timedelta(seconds=2)):
            assert not pending.exists()
            assert not Post.objects.with_actor(ACTOR).exists()


def test_schema_expiry_rebuilds_without_a_write(fixture):
    from django.contrib.contenttypes.models import ContentType

    from rebac.models import SchemaOverride, SchemaPermission

    _, _, visible, _ = fixture
    permission = SchemaPermission.objects.get(definition__resource_type="blog/post", name="read")
    now = timezone.now()
    SchemaOverride.objects.create(
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=permission.pk,
        kind="disable",
        expression="authenticated",
        reason="expiry regression",
        expires_at=now + timedelta(seconds=1),
    )
    with evaluator_scope():
        with patch("django.utils.timezone.now", return_value=now):
            assert not Post.objects.with_actor(ACTOR).exists()
        with patch("django.utils.timezone.now", return_value=now + timedelta(seconds=2)):
            assert Post.objects.with_actor(ACTOR).filter(pk=visible.pk).exists()


def test_savepoint_rollback_restores_index_visibility(fixture):
    active, _, _, hidden = fixture
    with (
        transaction.atomic(),
        evaluator_scope(),
    ):
        assert Post.objects.with_actor(ACTOR).count() == 1
        savepoint = transaction.savepoint()
        grant(active, hidden, "owner")
        assert Post.objects.with_actor(ACTOR).count() == 2
        transaction.savepoint_rollback(savepoint)
        assert Post.objects.with_actor(ACTOR).count() == 1

        transaction.savepoint_commit(savepoint)
