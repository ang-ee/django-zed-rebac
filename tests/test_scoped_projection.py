import pytest
from django.db.models import Count, Q, Sum
from django.test import override_settings

from rebac import (
    RelationshipTuple,
    SubjectRef,
    actor_context,
    anonymous_actor,
    backend,
    sudo,
    to_object_ref,
    write_relationships,
)
from rebac.backends import LocalBackend, reset_backend
from rebac.managers import RebacManager, RebacQuerySet
from rebac.schema import parse_zed
from tests.testapp.models import Post, SluggedPost

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")
SCHEMA = """
definition auth/user {}
definition blog/post {
    relation reader: auth/user
    relation writer: auth/user
    permission read = reader
    permission write = writer
}
definition blog/sluggedpost {
    relation reader: auth/user
    permission read = reader
}
"""


@pytest.fixture(params=["denormalized", "registry"])
def rows(db, request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        active = backend()
        assert isinstance(active, LocalBackend)
        active.set_schema(parse_zed(SCHEMA))
        with sudo(reason="projection fixtures"):
            first = Post.objects.create(title="first")
            second = Post.objects.create(title="second")
            third = Post.objects.create(title="third")
            slugged = SluggedPost.objects.create(slug="public-key", title="slugged")
        write_relationships(
            [
                RelationshipTuple(to_object_ref(first), "reader", ALICE),
                RelationshipTuple(to_object_ref(first), "writer", BOB),
                RelationshipTuple(to_object_ref(second), "reader", BOB),
                RelationshipTuple(to_object_ref(second), "writer", ALICE),
                RelationshipTuple(to_object_ref(third), "reader", ALICE),
                RelationshipTuple(to_object_ref(third), "reader", BOB),
                RelationshipTuple(to_object_ref(slugged), "reader", ALICE),
            ]
        )
        yield first, second, third, slugged
        reset_backend()


def test_scope_is_clone_and_survives_naive_sql_projection(rows):
    first, _second, _third, _ = rows
    original = Post.objects.with_actor(ALICE).filter(title__in=["first", "second"])
    scoped = original.scoped()
    assert scoped is not original
    assert original.query.where != scoped.query.where
    outer = Post._base_manager.filter(pk__in=scoped.values("pk"))
    assert list(outer.values_list("pk", flat=True)) == [first.pk]
    assert scoped.count() == 1


def test_actor_action_and_sudo_replace_old_scope_without_losing_user_predicate(rows):
    first, second, _third, _ = rows
    scoped = Post.objects.with_actor(ALICE).filter(title__in=["first", "second"]).scoped()
    assert list(scoped.values_list("pk", flat=True)) == [first.pk]
    assert list(scoped.with_actor(BOB).values_list("pk", flat=True)) == [second.pk]
    assert list(scoped.with_action("write").values_list("pk", flat=True)) == [second.pk]
    assert scoped.sudo(reason="test replace scope").count() == 2
    assert scoped.count() == 1


def test_combined_queries_replace_all_stale_scope_predicates(rows):
    _first, second, _third, _ = rows
    left = Post.objects.with_actor(ALICE).filter(title="first").scoped()
    right = Post.objects.with_actor(ALICE).filter(title="second").scoped()
    combined = (left | right).with_actor(BOB)
    assert list(combined.values_list("pk", flat=True)) == [second.pk]


def test_lazy_evaluated_queryset_clones_resolve_new_ambient_actor(rows):
    _first, second, _third, _ = rows
    queryset = Post.objects.filter(Q(title="first") | Q(title="second"))
    with actor_context(ALICE):
        assert queryset.count() == 1
    with actor_context(BOB):
        assert list(queryset.all().values_list("pk", flat=True)) == [second.pk]


def test_eager_scope_pins_ambient_actor_and_explicit_beats_ambient_sudo(rows):
    first, _second, third, _ = rows
    with actor_context(ALICE):
        scoped = Post.objects.scoped_for_aggregate()
    with actor_context(BOB):
        assert set(scoped.values_list("pk", flat=True)) == {first.pk, third.pk}
    with sudo(reason="ambient"):
        assert Post.objects.with_actor(ALICE).scoped_for_aggregate().count() == 2
        unscoped = Post.objects.scoped_for_aggregate()
    assert unscoped.count() == 3


@pytest.mark.parametrize("strict", [True, False])
def test_missing_actor_aggregate_fails_closed_and_can_be_rebound(rows, strict):
    with override_settings(REBAC_STRICT_MODE=strict):
        scoped = Post.objects.scoped_for_aggregate()
        assert scoped.aggregate(count=Count("pk"), total=Sum("pk")) == {"count": 0, "total": None}
        assert list(scoped.values("pk")) == []
        assert scoped.with_actor(ALICE).count() == 2


def test_anonymous_actor_is_not_missing_actor(rows):
    reset_backend()
    active = backend()
    assert isinstance(active, LocalBackend)
    active.set_schema(parse_zed("definition blog/post { permission read = anonymous }"))
    assert Post.objects.with_actor(anonymous_actor()).scoped_for_aggregate().count() == 3


def test_custom_identity_aggregate_counts_and_sum_preserve_logical_rows(rows):
    first, _second, third, _slugged = rows
    queryset = Post.objects.with_actor(ALICE).scoped_for_aggregate()
    assert queryset.aggregate(count=Count("pk"), total=Sum("pk")) == {
        "count": 2,
        "total": first.pk + third.pk,
    }
    slugs = SluggedPost.objects.with_actor(ALICE).scoped_for_aggregate().values("pk")
    assert list(SluggedPost._base_manager.filter(pk__in=slugs).values_list("slug", flat=True)) == [
        "public-key"
    ]


def test_queryset_subclass_and_database_survive(rows):
    class CustomQuerySet(RebacQuerySet[Post]):
        pass

    manager = RebacManager.from_queryset(CustomQuerySet)()
    manager.model = Post
    scoped = manager.using("default").with_actor(ALICE).scoped_for_aggregate()
    assert isinstance(scoped, CustomQuerySet)
    assert scoped.db == "default"
    assert scoped.count() == 2


def test_relationship_order_is_stable_for_limited_wire_rows(rows):
    from rebac.models import active_relationship_model

    model = active_relationship_model()
    for subject_id, relation, caveat in [
        ("b", "", ""),
        ("a", "member", ""),
        ("a", "", "limited"),
        ("a", "", ""),
    ]:
        model.objects.create(
            resource_type="test/item",
            resource_id="same",
            relation="reader",
            subject_type="auth/user",
            subject_id=subject_id,
            optional_subject_relation=relation,
            caveat_name=caveat,
        )
    ordered = model.objects.filter(resource_type="test/item").order_by_resource()
    wire = [(row.subject_id, row.optional_subject_relation, row.caveat_name) for row in ordered]
    assert wire == [("a", "", ""), ("a", "", "limited"), ("a", "member", ""), ("b", "", "")]
    assert [
        (row.subject_id, row.optional_subject_relation, row.caveat_name) for row in ordered[:2]
    ] == wire[:2]


@pytest.mark.parametrize("operation", ["union", "intersection", "difference"])
def test_sql_set_combinations_scope_each_operand_and_rebind(rows, operation):
    first, second, third, _ = rows
    base = Post.objects.with_actor(ALICE).scoped_for_aggregate()
    other = Post.objects.filter(title__in=["first", "second"])
    combined = getattr(base, operation)(other)
    expected = {"union": {first.pk, third.pk}, "intersection": {first.pk}, "difference": {third.pk}}
    assert set(combined.values_list("pk", flat=True)) == expected[operation]
    rebound = combined.with_actor(BOB)
    expected_bob = {
        "union": {second.pk, third.pk},
        "intersection": {second.pk},
        "difference": {third.pk},
    }
    # Plain manager SQL compilation cannot add missing permission restrictions.
    outer = Post._base_manager.filter(pk__in=rebound.values("pk"))
    assert set(outer.values_list("pk", flat=True)) == expected_bob[operation]
    assert base.count() == 2
    with sudo(reason="verify operand remains unchanged"):
        assert other.count() == 2


def test_eager_or_unscoped_operand_keeps_left_actor_policy(rows):
    first, _second, third, _ = rows
    left = Post.objects.with_actor(ALICE).filter(title="first").scoped()
    right = Post.objects.filter(title__in=["second", "third"])
    combined = left | right
    outer = Post._base_manager.filter(pk__in=combined.values("pk"))
    assert set(outer.values_list("pk", flat=True)) == {first.pk, third.pk}


def test_lazy_sql_union_scopes_before_materialization(rows):
    first, _second, third, _ = rows
    left = Post.objects.with_actor(ALICE).filter(title="first")
    right = Post.objects.filter(title__in=["second", "third"])
    assert set(left.union(right).values_list("pk", flat=True)) == {first.pk, third.pk}


def test_values_list_clone_and_get_keep_scalar_and_tuple_shapes(rows):
    first, _second, _third, _ = rows
    values = Post.objects.with_actor(ALICE).filter(pk=first.pk).values_list("title", flat=True)
    assert values.get() == "first"
    assert values.all().get() == "first"
    assert isinstance(values, RebacQuerySet)
    assert values.scoped().get() == "first"
    assert Post.objects.with_actor(ALICE).filter(pk=first.pk).values_list("title", "pk").get() == (
        "first",
        first.pk,
    )


def test_lazy_sql_combination_does_not_mutate_its_operands(rows):
    _first, _second, _third, _ = rows
    left = Post.objects.with_actor(ALICE).filter(title="first")
    right = Post.objects.filter(title__in=["second", "third"])
    assert left.union(right).count() == 2
    with sudo(reason="unchanged original query"):
        assert set(right.values_list("title", flat=True)) == {"second", "third"}


def test_empty_left_boolean_combination_retains_left_actor(rows):
    first, _second, third, _ = rows
    left = Post.objects.with_actor(ALICE).scoped().none()
    right = Post.objects.with_actor(BOB).scoped()
    assert set((left | right).values_list("pk", flat=True)) == {first.pk, third.pk}
    assert right.count() == 2


def test_boolean_combination_cannot_escape_to_plain_manager(rows):
    left = Post.objects.with_actor(ALICE).scoped().none()
    with pytest.raises(TypeError, match="require REBAC querysets"):
        left | Post._base_manager.all()
