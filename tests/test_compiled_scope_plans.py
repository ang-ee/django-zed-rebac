"""One SQL construction per operation; execution and invalidation stay live."""

from collections import Counter
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
    PermissionDepthExceeded,
    RelationshipTuple,
    SubjectRef,
    backend,
    evaluator_scope,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.test_recursive_queryscope import ACTOR, OUTSIDER, chain, grant, schema_context
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["denormalized", "registry"])
def fixture(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        call_command("rebac", "sync", stdout=StringIO())
        active = backend()
        with sudo(reason="compiled plan regression"):
            folder = Folder.objects.create(name="visible")
            visible = Post.objects.create(title="visible", folder=folder)
            hidden = Post.objects.create(title="hidden", folder=folder)
        grant(active, folder, "owner")
        grant(active, visible, "owner")
        yield active, folder, visible, hidden
        reset_backend()


def test_one_construction_across_querysets_filters_and_prefetches(fixture):
    active, folder, visible, _ = fixture
    calls = Counter()
    original = active.queryset_filter

    def compile(**kwargs):
        calls[(kwargs["model"], kwargs["action"], kwargs["subject"])] += 1
        return original(**kwargs)

    with patch.object(active, "queryset_filter", side_effect=compile), evaluator_scope():
        for _ in range(3):
            posts = Post.objects.with_actor(ACTOR)
            assert posts.count() == 1
            assert posts.filter(title="visible").exists()
            assert list(posts.values_list("pk", flat=True)) == [visible.pk]
            assert list(posts.scoped_for_aggregate().values_list("pk", flat=True)) == [visible.pk]
            parent = (
                Folder.objects.with_actor(ACTOR)
                .prefetch_related(Prefetch("posts", queryset=posts))
                .get(pk=folder.pk)
            )
            assert [row.pk for row in parent.posts.all()] == [visible.pk]
        assert calls == {(Post, "read", ACTOR): 1, (Folder, "read", ACTOR): 1}
        assert not Post.objects.with_actor(OUTSIDER).exists()
        assert Post.objects.with_actor(ACTOR).with_action("write").exists()
        assert calls[(Post, "read", OUTSIDER)] == 1
        assert calls[(Post, "write", ACTOR)] == 1


def test_no_scope_keeps_direct_construction(fixture):
    active, *_ = fixture
    with patch.object(active, "queryset_filter", wraps=active.queryset_filter) as compile:
        for _ in range(3):
            assert Post.objects.with_actor(ACTOR).exists()
    assert compile.call_count == 3


@pytest.mark.parametrize("change", ["relationship", "sync", "rollback", "invalidate", "boundary"])
def test_existing_invalidation_owner_rebuilds(fixture, change):
    active, _, visible, hidden = fixture
    original = type(active).queryset_filter
    with (
        evaluator_scope() as evaluator,
        patch.object(
            type(active), "queryset_filter", autospec=True, side_effect=original
        ) as compile,
    ):
        assert Post.objects.with_actor(ACTOR).count() == 1
        assert compile.call_count == 1
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
        before = compile.call_count
        expected = 2 if change == "relationship" else 1
        assert Post.objects.with_actor(ACTOR).count() == expected
        assert compile.call_count == before + 1
        assert Post.objects.with_actor(ACTOR).filter(pk=visible.pk).exists()
        assert compile.call_count == before + 1


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


def test_plan_is_opaque_to_caller_cloning(fixture):
    active, *_ = fixture
    from rebac.backends.scope_plan import StandaloneIds

    with evaluator_scope() as evaluator:
        plan = evaluator.compiled_scope_plan(
            active, model=Post, subject=ACTOR, action="read", using="default"
        )
        checked = plan.children[0]
        ids = checked.expressions[0].children[0][1]
        assert isinstance(ids, StandaloneIds)
        with patch.object(ids._id_query, "clone", side_effect=AssertionError("cloned plan")):
            query = Post.objects.with_actor(ACTOR).scoped()
            assert query.filter(title="visible").count() == 1
            assert list(Post._base_manager.filter(pk__in=query.values("pk")))


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
def test_recursive_enumeration_and_bulk_guard_raise_at_reachable_bound(storage, backing):
    with (
        override_settings(REBAC_DEPTH_LIMIT=2),
        schema_context(storage, "folder", backing) as (active, member, hop, action),
    ):
        rows = chain(active, hop, backing, 2)
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
        active.set_schema(
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
            )
        )
        for subject in (ACTOR, OUTSIDER):
            with pytest.raises(PermissionDepthExceeded, match="Depth limit 2 exceeded"):
                list(active.accessible(subject=subject, action=action, resource_type="blog/folder"))
            with pytest.raises(PermissionDepthExceeded, match="Depth limit 2 exceeded"):
                Folder.objects.with_actor(subject).filter(pk=overflow.pk).update(name="denied")
        assert Folder._base_manager.get(pk=overflow.pk).name == "overflow"


def test_expiration_is_bound_at_execution_even_on_a_reused_plan(fixture):
    active, _, visible, _ = fixture
    active.set_schema(
        parse_zed("""
        use expiration
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user with expiration
            permission read = viewer
        }
    """)
    )
    now = timezone.now()
    active.write_relationships(
        [
            RelationshipTuple(
                to_object_ref(visible), "viewer", ACTOR, expires_at=now + timedelta(seconds=1)
            )
        ]
    )
    with (
        evaluator_scope(),
        patch.object(active, "queryset_filter", wraps=active.queryset_filter) as compile,
    ):
        pending = Post.objects.with_actor(ACTOR).scoped()
        with patch("django.utils.timezone.now", return_value=now):
            assert pending.exists()
        with patch("django.utils.timezone.now", return_value=now + timedelta(seconds=2)):
            assert not pending.exists()
            assert not Post.objects.with_actor(ACTOR).exists()
        assert compile.call_count == 1


def test_schema_expiry_rebuilds_without_a_write(fixture):
    from django.contrib.contenttypes.models import ContentType

    from rebac.models import SchemaOverride, SchemaPermission

    active, _, visible, _ = fixture
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
    active = backend()
    with (
        evaluator_scope(),
        patch.object(active, "queryset_filter", wraps=active.queryset_filter) as compile,
    ):
        with patch("django.utils.timezone.now", return_value=now):
            assert not Post.objects.with_actor(ACTOR).exists()
        with patch("django.utils.timezone.now", return_value=now + timedelta(seconds=2)):
            assert Post.objects.with_actor(ACTOR).filter(pk=visible.pk).exists()
        assert compile.call_count == 2


def test_reused_plan_validates_new_frontiers_and_caller_filters(fixture):
    active, folder, _, _ = fixture
    with override_settings(REBAC_DEPTH_LIMIT=1), evaluator_scope():
        pending = Folder.objects.with_actor(OUTSIDER).scoped()
        assert not pending.exists()
        # Create a cycle after the plan was built. No frontier answer may be
        # retained, even by an already-scoped queryset.
        active.write_relationships(
            [
                RelationshipTuple(
                    to_object_ref(folder), "parent", SubjectRef.of("blog/folder", str(folder.pk))
                )
            ]
        )
        assert not pending.filter(pk=-1).exists()
        with pytest.raises(PermissionDepthExceeded):
            pending.filter(pk=folder.pk).exists()


def test_duplicate_boolean_arms_emit_once(fixture):
    from rebac.backends.local_query import LocalQueryScope

    active, _, visible, _ = fixture
    active.set_schema(
        parse_zed("""
        definition auth/user {}
        definition blog/post {
            relation owner: auth/user
            relation other: auth/user
            permission alias = owner + other
            permission read = owner + other
            permission duplicate = ((owner + other) + alias) + owner
        }
    """)
    )
    sql = []
    for action in ("read", "duplicate"):
        predicate = LocalQueryScope(active, ACTOR, "default").predicate(Post, action, "blog/post")
        rows = Post._base_manager.filter(predicate)
        assert list(rows.values_list("pk", flat=True)) == [visible.pk]
        sql.append(rows.query.sql_with_params())
    assert sql[0] == sql[1]


def test_key_includes_depth_and_backend_identity_and_teardown_discards_plans(fixture):
    from rebac import LocalBackend, PermissionEvaluator

    active, *_ = fixture
    evaluator = PermissionEvaluator(max_size=2)
    with evaluator_scope(evaluator):

        def plan(local):
            return evaluator.compiled_scope_plan(
                local, model=Post, subject=ACTOR, action="read", using="default"
            )

        first = plan(active)
        assert plan(active) is first
        with override_settings(REBAC_DEPTH_LIMIT=2):
            assert plan(active) is not first
        assert plan(LocalBackend()) is not first
        assert len(evaluator._plan_cache) == 2
    assert not evaluator._plan_cache


def test_savepoint_rollback_invalidates_same_backend_plan(fixture):
    active, _, _, hidden = fixture
    with (
        transaction.atomic(),
        evaluator_scope(),
        patch.object(active, "queryset_filter", wraps=active.queryset_filter) as compile,
    ):
        assert Post.objects.with_actor(ACTOR).count() == 1
        savepoint = transaction.savepoint()
        grant(active, hidden, "owner")
        assert Post.objects.with_actor(ACTOR).count() == 2
        before = compile.call_count
        transaction.savepoint_rollback(savepoint)
        assert Post.objects.with_actor(ACTOR).count() == 1
        assert compile.call_count == before + 1
        transaction.savepoint_commit(savepoint)
