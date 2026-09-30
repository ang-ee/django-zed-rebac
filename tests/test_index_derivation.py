"""Derivation regressions against source facts, the reference model, and the walker."""

import re
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from rebac import ObjectRef, RelationshipTuple, SchemaError, SubjectRef, backend, sudo
from rebac.backends import reset_backend
from rebac.index import conditions, read
from rebac.index.derive import derive_memberships, derive_nodes
from rebac.index.program import program_for
from rebac.index.project import project_edges
from rebac.index.rebuild import rebuild
from rebac.models.index import IndexCover, IndexEdge, IndexMember
from rebac.schema import parse_zed
from rebac.types import RelationshipFilter
from tests.backend_setup import STORAGE_TIERS
from tests.index_harness import assert_index_matches, assert_no_drift, assert_scope_matches, seed

pytestmark = pytest.mark.django_db


@pytest.fixture(params=("denormalized", "registry"))
def install(request, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    settings.REBAC_DEPTH_LIMIT = 128  # The walker oracle needs headroom; the index does not use it.
    reset_backend()

    def apply(schema):
        active = backend()
        active.set_schema(parse_zed(schema))
        rebuild(using="default")
        return active

    return apply


def user(name):
    return SubjectRef.of("auth/user", name)


def doc(name="d"):
    return ObjectRef("test/doc", name)


BASE = """
definition auth/user {}
definition auth/anonymous {}
definition test/group {
    relation member: auth/user | auth/user:* | test/group#member
}
definition test/doc {
    relation a: auth/user | auth/user:* | test/group#member
    relation b: auth/user | auth/user:* | test/group#member
    relation c: auth/user | auth/user:* | test/group#member
    relation parent: test/doc | test/doc#a
    permission union = a + b
    permission intersection = a & b
    permission difference = a - b
    permission nested = (a - b) & c
    permission hole = a - (b - c)
    permission public = authenticated - b
    permission rescue = (authenticated - b) + c
    permission both_classes = authenticated & a
    permission inherited = parent->difference
    permission read = difference + parent->read
}
"""


@pytest.mark.parametrize("install", STORAGE_TIERS, indirect=True)
def test_all_setop_lanes_and_finite_exclusion(install):
    install(BASE)
    seed(
        [
            "test/group:g#member@auth/user:alice",
            "test/group:g#member@auth/user:bob",
            "test/group:h#member@auth/user:alice",
            "test/group:h#member@auth/user:carol",
            "test/doc:d#a@test/group:g#member",
            "test/doc:d#b@auth/user:alice",
            "test/doc:d#c@test/group:h#member",
            "test/doc:child#parent@test/doc:d#a",
        ]
    )
    rebuild(using="default")
    assert_index_matches(
        subjects=[
            user("alice"),
            user("bob"),
            user("carol"),
            user("unknown"),
            SubjectRef.of("test/group", "g", "member"),
            SubjectRef.of("auth/anonymous", "*"),
        ],
        resources=[doc(), doc("child")],
        actions=[
            "a",
            "union",
            "intersection",
            "difference",
            "nested",
            "hole",
            "public",
            "rescue",
            "inherited",
            "read",
        ],
    )
    assert not read.check(
        resource=doc(), action="difference", actor=user("alice"), context=None, using="default"
    ).allowed
    assert read.check(
        resource=doc(), action="difference", actor=user("bob"), context=None, using="default"
    ).allowed


@pytest.mark.parametrize(
    "left_group,right_group", [(False, False), (True, False), (False, True), (True, True)]
)
def test_intersection_join_lanes_match_general_lane(install, left_group, right_group):
    install("""
        definition auth/user {}
        definition test/group { relation member: auth/user }
        definition test/doc {
            relation grp: auth/user | test/group#member
            relation approved: auth/user | test/group#member
            permission edit = grp & approved
        }
    """)
    rows = []
    for relation, group, members, grouped in (
        ("grp", "g", ("alice", "bob"), left_group),
        ("approved", "h", ("bob", "carol") if right_group else ("bob",), right_group),
    ):
        if grouped:
            rows.append(f"test/doc:d#{relation}@test/group:{group}#member")
            rows.extend(f"test/group:{group}#member@auth/user:{actor}" for actor in members)
        else:
            rows.extend(f"test/doc:d#{relation}@auth/user:{actor}" for actor in members)
    seed(rows)
    subjects = [user(name) for name in ("alice", "bob", "carol")]
    assert_index_matches(subjects=subjects, resources=[doc()], actions=["edit"])
    assert (
        set(
            read.accessible_ids(
                resource_type="test/doc", action="edit", actor=user("alice"), using="default"
            )
        )
        == set()
    )
    assert set(
        read.accessible_ids(
            resource_type="test/doc", action="edit", actor=user("bob"), using="default"
        )
    ) == {"d"}

    assert IndexCover.objects.filter(node="edit", site__gt="").exists()
    assert_no_drift()


@pytest.mark.parametrize("conditional", ["right-cover", "left-member", "right-member"])
def test_intersection_caveats_do_not_borrow_plain_join_rows(install, conditional):
    active = install("""
        caveat gate(enabled bool) { enabled }
        definition auth/user {}
        definition test/group { relation member: auth/user | auth/user with gate }
        definition test/doc {
            relation grp: auth/user | test/group#member
            relation approved: auth/user | auth/user with gate | test/group#member
            permission edit = grp & approved
        }
    """)
    rows = []
    for relation, side in (("grp", "left"), ("approved", "right")):
        if conditional == f"{side}-member":
            rows.extend(
                [
                    RelationshipTuple(doc(), relation, SubjectRef.of("test/group", "g", "member")),
                    RelationshipTuple(ObjectRef("test/group", "g"), "member", user("alice")),
                    RelationshipTuple(ObjectRef("test/group", "g"), "member", user("bob"), "gate"),
                ]
            )
        else:
            rows.extend(
                [
                    RelationshipTuple(doc(), relation, user("alice")),
                    RelationshipTuple(
                        doc(),
                        relation,
                        user("bob"),
                        "gate" if conditional == "right-cover" and side == "right" else "",
                    ),
                ]
            )
    active.write_relationships(rows)
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc()],
        actions=["edit"],
        contexts=[None, {"enabled": False}, {"enabled": True}],
    )
    result = read.check(
        resource=doc(), action="edit", actor=user("bob"), context=None, using="default"
    )
    assert result.conditional_on == ("enabled",)
    assert not IndexCover.objects.filter(
        node="edit", holder__object_id="bob", condition_key=""
    ).exists()
    assert (
        set(
            read.accessible_ids(
                resource_type="test/doc", action="edit", actor=user("bob"), using="default"
            )
        )
        == set()
    )
    assert_no_drift()


def test_arrow_plain_and_caveated_edges_to_same_target_stay_separate(install):
    from tests.testapp.models import Post

    active = install("""
        caveat gate(enabled bool) { enabled }
        definition auth/user {}
        definition test/folder { relation viewer: auth/user permission read = viewer }
        definition blog/post {
            relation parent: test/folder | test/folder with gate
            permission read = parent->read
        }
    """)
    with sudo(reason="mixed arrow fixtures"):
        plain = Post.objects.create(title="plain")
        caveated = Post.objects.create(title="caveated")
    resources = [ObjectRef("blog/post", str(row.pk)) for row in (plain, caveated)]
    active.write_relationships(
        [
            RelationshipTuple(ObjectRef("test/folder", "one"), "viewer", user("alice")),
            RelationshipTuple(resources[0], "parent", SubjectRef.of("test/folder", "one")),
            RelationshipTuple(resources[1], "parent", SubjectRef.of("test/folder", "one"), "gate"),
        ]
    )
    assert_index_matches(
        subjects=[user("alice")],
        resources=resources,
        actions=["read"],
        contexts=[None, {"enabled": False}, {"enabled": True}],
    )
    assert_scope_matches(Post, actor=user("alice"), action="read")
    assert set(
        Post._base_manager.filter(
            read.scope_q(Post, action="read", actor=user("alice"), using="default")
        ).values_list("pk", flat=True)
    ) == {plain.pk}
    assert read.check(
        resource=resources[1], action="read", actor=user("alice"), context=None, using="default"
    ).conditional_on == ("enabled",)
    assert_no_drift()


def test_nested_conditional_ban_propagates_both_membership_lanes(install):
    from tests.testapp.models import Post

    active = install("""
        caveat gate(enabled bool) { enabled }
        definition auth/user {}
        definition test/group {
            relation member: auth/user | auth/user with gate | test/group#member
        }
        definition blog/post {
            relation banned: test/group#member
            permission view = authenticated - banned
        }
    """)
    with sudo(reason="nested membership fixtures"):
        post = Post.objects.create(title="conditional ban")
    resource = ObjectRef("blog/post", str(post.pk))
    active.write_relationships(
        [
            RelationshipTuple(resource, "banned", SubjectRef.of("test/group", "bad", "member")),
            RelationshipTuple(
                ObjectRef("test/group", "bad"),
                "member",
                SubjectRef.of("test/group", "sub", "member"),
            ),
            RelationshipTuple(ObjectRef("test/group", "sub"), "member", user("alice")),
            RelationshipTuple(ObjectRef("test/group", "sub"), "member", user("bob"), "gate"),
        ]
    )
    assert IndexMember.objects.filter(
        set__object_id="bad", member__object_id="alice", condition_key=""
    ).exists()
    assert IndexMember.objects.filter(
        set__object_id="bad", member__object_id="bob", condition_key__gt=""
    ).exists()
    assert not IndexMember.objects.filter(
        set__object_id="bad", member__object_id="bob", condition_key=""
    ).exists()
    assert_index_matches(
        subjects=[user("alice"), user("bob"), user("carol")],
        resources=[resource],
        actions=["view", "banned"],
        contexts=[None, {"enabled": False}, {"enabled": True}],
    )
    assert_scope_matches(Post, actor=user("bob"), action="view")
    assert not Post._base_manager.filter(
        read.scope_q(Post, action="view", actor=user("bob"), using="default")
    ).exists()
    assert read.check(
        resource=resource, action="view", actor=user("bob"), context=None, using="default"
    ).conditional_on == ("enabled",)
    assert_no_drift()


def test_conditional_outer_membership_keeps_each_inner_formula_on_its_member(install):
    active = install("""
        caveat outer_gate(outer bool) { outer }
        caveat inner_gate(inner bool) { inner }
        definition auth/user {}
        definition test/group {
            relation member: auth/user | auth/user with inner_gate | test/group#member with outer_gate
        }
        definition test/doc {
            relation viewer: test/group#member
            permission read = viewer
        }
    """)
    active.write_relationships(
        [
            RelationshipTuple(doc(), "viewer", SubjectRef.of("test/group", "outer", "member")),
            RelationshipTuple(
                ObjectRef("test/group", "outer"),
                "member",
                SubjectRef.of("test/group", "inner", "member"),
                "outer_gate",
            ),
            RelationshipTuple(ObjectRef("test/group", "inner"), "member", user("alice")),
            RelationshipTuple(
                ObjectRef("test/group", "inner"), "member", user("bob"), "inner_gate"
            ),
        ]
    )
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc()],
        actions=["read"],
        contexts=[
            None,
            {"outer": True},
            {"outer": False},
            {"outer": True, "inner": True},
            {"outer": True, "inner": False},
        ],
    )
    assert read.check(
        resource=doc(), action="read", actor=user("alice"), context=None, using="default"
    ).conditional_on == ("outer",)
    assert read.check(
        resource=doc(), action="read", actor=user("bob"), context={"outer": True}, using="default"
    ).conditional_on == ("inner",)
    assert_no_drift()


def test_wildcard_members_and_subject_set_atoms(install):
    install(BASE)
    seed(
        [
            "test/group:all#member@auth/user:*",
            "test/group:outer#member@test/group:all#member",
            "test/doc:d#a@test/group:outer#member",
            "test/doc:d#b@test/group:all#member",
            "test/doc:d#c@auth/user:alice",
        ]
    )
    rebuild(using="default")
    assert_index_matches(
        subjects=[
            user("never-stored"),
            user("alice"),
            SubjectRef.of("test/group", "all", "member"),
            SubjectRef.of("test/group", "outer", "member"),
            SubjectRef.of("auth/user", "alice", "member"),
        ],
        resources=[doc()],
        actions=["a", "b", "intersection", "difference", "hole", "rescue"],
    )


def test_class_intersection_keeps_actor_shape(install):
    install(BASE)
    seed(["test/doc:d#a@auth/user:*"])
    rebuild(using="default")
    assert_index_matches(
        subjects=[
            user("alice"),
            user(""),
            SubjectRef.of("auth/user", "alice", "member"),
            SubjectRef.of("auth/anonymous", "*"),
            SubjectRef.of("auth/anonymous", "other"),
        ],
        resources=[doc()],
        actions=["a", "both_classes", "public"],
    )


def test_anonymous_singleton_and_same_type_wildcard_are_distinct(install):
    install("""
        definition auth/anonymous {}
        definition test/doc { relation a: auth/anonymous:*
            permission literal = anonymous
            permission wildcard = a
        }
    """)
    seed(["test/doc:d#a@auth/anonymous:*"])
    rebuild(using="default")
    assert_index_matches(
        subjects=[SubjectRef.of("auth/anonymous", "*"), SubjectRef.of("auth/anonymous", "other")],
        resources=[doc()],
        actions=["literal", "wildcard"],
    )


def test_anonymous_type_class_intersection_excludes_singleton_and_empty_id(install):
    install("""
        definition auth/anonymous {}
        definition test/doc {
            relation a: auth/anonymous:*
            permission read = authenticated & a
            permission remainder = authenticated - a
        }
    """)
    seed(["test/doc:d#a@auth/anonymous:*"])
    rebuild(using="default")
    assert_index_matches(
        subjects=[
            SubjectRef.of("auth/anonymous", id_, suffix)
            for id_ in ("", "*", "other")
            for suffix in ("", "member")
        ],
        resources=[doc()],
        actions=["read", "remainder"],
    )


@pytest.mark.parametrize("conditional_path", ["tuple", "membership"])
def test_conditional_right_cover_hole_is_a_definite_regrant(install, conditional_path):
    active = install("""
        caveat gate(enabled bool) { enabled }
        definition auth/user {}
        definition test/group { relation member: auth/user with gate }
        definition test/doc {
            relation a: auth/user with gate | test/group#member
            relation b: auth/user
            permission read = authenticated - (a - b)
        }
    """)
    rows = [RelationshipTuple(doc(), "b", user("alice"))]
    if conditional_path == "tuple":
        rows.append(RelationshipTuple(doc(), "a", user("alice"), "gate"))
    else:
        rows.extend(
            [
                RelationshipTuple(doc(), "a", SubjectRef.of("test/group", "g", "member")),
                RelationshipTuple(ObjectRef("test/group", "g"), "member", user("alice"), "gate"),
            ]
        )
    active.write_relationships(rows)
    rebuild(using="default")
    assert_index_matches(
        subjects=[user("alice"), user("unknown")],
        resources=[doc()],
        actions=["read"],
        contexts=[None, {"enabled": False}, {"enabled": True}],
    )
    assert read.check(
        resource=doc(), action="read", actor=user("alice"), context=None, using="default"
    ).allowed


def test_membership_recursion_uses_both_join_inputs(install):
    install(BASE)
    seed(
        [
            "test/group:outer#member@test/group:left#member",
            "test/group:left#member@test/group:right#member",
            "test/group:right#member@auth/user:alice",
            "test/group:right#member@test/group:left#member",
            "test/doc:d#a@test/group:outer#member",
        ]
    )
    rebuild(using="default")
    assert_index_matches(subjects=[user("alice")], resources=[doc()], actions=["a", "union"])
    assert IndexMember.objects.filter(member__object_id="alice", set__object_id="outer").exists()
    assert not read.check(
        resource=doc(), action="a", actor=user("outsider"), context=None, using="default"
    ).allowed


def test_grouped_conditional_exclusion_does_not_visit_a_short_circuited_hole(install):
    active = install("""
        caveat left_gate(left bool) { left }
        caveat middle_gate(middle bool) { middle }
        caveat hole_gate(hole bool) { hole }
        definition auth/user {}
        definition test/doc {
            relation a: auth/user with left_gate
            relation b: auth/user with middle_gate
            relation c: auth/user with hole_gate
            permission read = a - (b - c)
        }
    """)
    active.write_relationships(
        [
            RelationshipTuple(doc(), relation, user("alice"), caveat)
            for relation, caveat in (("a", "left_gate"), ("b", "middle_gate"), ("c", "hole_gate"))
        ]
    )
    assert_index_matches(
        subjects=[user("alice")],
        resources=[doc()],
        actions=["read"],
        contexts=[{"middle": False}, {"middle": True}, None],
    )


def test_cofinite_covers_travel_through_recursive_data_cycle(install):
    install(BASE)
    seed(
        [
            "test/doc:root#a@auth/user:*",
            "test/doc:root#b@auth/user:blocked",
            "test/doc:root#parent@test/doc:child",
            "test/doc:child#parent@test/doc:root",
        ]
    )
    rebuild(using="default")
    assert_index_matches(
        subjects=[user("allowed")], resources=[doc("root"), doc("child")], actions=["read"]
    )
    for resource in [doc("root"), doc("child")]:
        assert not read.check(
            resource=resource, action="read", actor=user("blocked"), context=None, using="default"
        ).allowed
    assert IndexCover.objects.filter(node="read").exclude(site="").exists()


def test_type_level_constant_minus_concrete_ban(install):
    install("""
        definition auth/user {}
        definition blog/post {
            relation constant: auth/user // rebac:const=alice
            relation banned: auth/user
            permission read = constant - banned
        }
    """)
    seed(["blog/post:banned#banned@auth/user:alice"])
    rebuild(using="default")
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[ObjectRef("blog/post", value) for value in ("banned", "other", "nonexistent")],
        actions=["constant", "read"],
    )
    assert IndexCover.objects.filter(scope__relation="$type", node="read", site__gt="").exists()


def test_unfiltered_constant_supplies_referenced_concrete_set(install):
    install("""
        definition auth/user {}
        definition blog/post { relation member: auth/user // rebac:const=alice
        }
        definition test/doc {
            relation a: blog/post#member
            permission read = a
        }
    """)
    seed(["test/doc:d#a@blog/post:virtual#member"])
    rebuild(using="default")
    assert_index_matches(
        subjects=[user("alice"), user("bob"), SubjectRef.of("blog/post", "virtual", "member")],
        resources=[doc()],
        actions=["a", "read"],
    )
    assert IndexMember.objects.filter(set__object_id="virtual", member__object_id="alice").exists()


def test_arrow_includes_type_level_target_covers_and_ignores_suffix(install):
    install("""
        definition auth/user {}
        definition test/target {
            relation member: auth/user
            permission read = authenticated
        }
        definition test/doc {
            relation parent: test/target#member
            permission read = parent->read
        }
    """)
    seed(["test/doc:d#parent@test/target:virtual#member"])
    rebuild(using="default")
    assert_index_matches(
        subjects=[user("unknown"), SubjectRef.of("test/target", "set", "member")],
        resources=[doc()],
        actions=["read"],
    )


CAVEATED = """
caveat ca(x bool) { x }
caveat cb(y bool) { y }
definition auth/user {}
definition test/group { relation member: auth/user with cb }
definition test/doc {
    relation a: auth/user with ca | test/group#member with ca
    relation b: auth/user with cb | test/group#member
    relation parent: test/doc with ca
    permission union = a + b
    permission intersection = a & b
    permission difference = a - b
    permission missing = a + (a & b)
    permission deny = authenticated - b
    permission arrow = parent->difference
}
"""


@pytest.mark.parametrize("install", STORAGE_TIERS, indirect=True)
def test_conditions_in_all_positions_and_membership_alternatives(install):
    active = install(CAVEATED)
    seed(["test/doc:root#b@test/group:g#member"])
    active.write_relationships(
        [
            RelationshipTuple(doc("root"), "a", user("alice"), "ca"),
            RelationshipTuple(doc("root"), "a", SubjectRef.of("test/group", "g", "member"), "ca"),
            RelationshipTuple(doc("root"), "b", user("alice"), "cb"),
            RelationshipTuple(ObjectRef("test/group", "g"), "member", user("alice"), "cb"),
            RelationshipTuple(doc("child"), "parent", SubjectRef.of("test/doc", "root"), "ca"),
        ]
    )
    rebuild(using="default")
    assert_index_matches(
        subjects=[user("alice"), user("unknown")],
        resources=[doc("root"), doc("child")],
        actions=["union", "intersection", "difference", "missing", "deny", "arrow"],
        contexts=[
            None,
            {"x": True},
            {"y": False},
            {"x": True, "y": True},
            {"x": True, "y": False},
            {"x": False, "y": True},
        ],
    )
    result = read.check(
        resource=doc("root"), action="missing", actor=user("alice"), context=None, using="default"
    )
    assert result.conditional_on == ("x", "y")


def test_formula_no_absorption_or_embedded_expression():
    schema = parse_zed("caveat ca(x bool) { x } caveat cb(y bool) { y }")
    a, b = conditions.leaf("ca", {}), conditions.leaf("cb", {})
    formula = conditions.or_(a, conditions.and_(a, b))
    assert conditions.size(formula) == 3
    # The stored formula keeps both paths. What it needs is what its value
    # depends on: the second path holds only where the first one does.
    assert conditions.evaluate(formula, schema, None) == (None, frozenset({"x"}))
    assert conditions.evaluate(conditions.and_(a, b), schema, None) == (
        None,
        frozenset({"x", "y"}),
    )
    assert len(conditions.key(formula)) == 64
    assert conditions.key(conditions.leaf("ca", {"z": 1, "a": 2})) == conditions.key(
        conditions.leaf("ca", {"a": 2, "z": 1})
    )
    replaced = parse_zed("caveat ca(x bool) { !x } caveat cb(y bool) { y }")
    assert conditions.evaluate(a, schema, {"x": True})[0] is True
    assert conditions.evaluate(a, replaced, {"x": True})[0] is False


def test_condition_limit_combines_rows(install):
    active = install("""
        caveat enabled(flag bool) { flag }
        definition auth/user {}
        definition test/doc { relation a: auth/user with enabled
            relation b: auth/user with enabled
            relation c: auth/user with enabled
            permission read = a + b + c
        }
    """)
    active.write_relationships(
        [
            RelationshipTuple(doc(), relation, user("alice"), "enabled", {"tag": n})
            for n, relation in enumerate(("a", "b", "c"))
        ]
    )
    with override_settings(REBAC_INDEX_CONDITION_LIMIT=2):
        with pytest.raises(SchemaError, match="REBAC_INDEX_CONDITION_LIMIT"):
            rebuild(using="default")
    rebuild(using="default")
    assert_index_matches(
        subjects=[user("alice")],
        resources=[doc()],
        actions=["read"],
        contexts=[None, {"flag": True}],
    )


def test_condition_limit_counts_distinct_formulas_for_one_grant_key(install):
    active = install("""
        caveat gate(enabled bool) { enabled }
        definition auth/user {}
        definition test/doc {
            relation a: auth/user | auth/user with gate
            relation b: auth/user with gate
            permission read = a + b
        }
    """)
    active.write_relationships(
        [
            RelationshipTuple(doc(), "a", user("alice")),
            RelationshipTuple(doc(), "a", user("bob"), "gate", {"tag": 1}),
            RelationshipTuple(doc(), "b", user("bob"), "gate", {"tag": 2}),
        ]
    )
    assert_index_matches(
        subjects=[user("alice"), user("bob"), user("carol"), user("dave")],
        resources=[doc()],
        actions=["read"],
        contexts=[None, {"enabled": False}, {"enabled": True}],
    )
    assert_no_drift()
    assert IndexCover.objects.filter(node="read", condition_key__gt="").count() == 2
    with override_settings(REBAC_INDEX_CONDITION_LIMIT=1):
        with pytest.raises(SchemaError, match="REBAC_INDEX_CONDITION_LIMIT"):
            rebuild(using="default")
    assert_no_drift()


def test_memberships_are_only_materialized_for_declared_usersets(install):
    install(BASE)
    seed(
        [
            "test/group:g#member@auth/user:alice",
            "test/doc:d#a@test/group:g#member",
            "test/doc:d#b@auth/user:bob",
            "test/doc:d#parent@test/doc:other",
            "test/doc:other#a@auth/user:carol",
        ]
    )
    # BASE references doc#a as a userset, but neither b nor parent. Relations
    # remain directly checkable even when they have no duplicated membership.
    assert set(IndexMember.objects.values_list("set__type", "set__relation")) == {
        ("test/group", "member"),
        ("test/doc", "a"),
    }
    assert not IndexMember.objects.filter(set__relation__in=("b", "parent")).exists()
    assert_index_matches(
        subjects=[user("alice"), user("bob"), user("carol")],
        resources=[doc(), doc("other")],
        actions=["a", "b", "parent", "intersection", "read"],
    )
    assert_no_drift()


def test_maximum_internal_node_name_derives_and_overflow_fails_before_writes(install):
    name = "p" * 51
    source = f"""
        definition auth/user {{}}
        definition test/doc {{ relation a: auth/user relation b: auth/user
            permission {name} = (a + b) & a
        }}
    """
    active = install(source)
    seed(["test/doc:d#a@auth/user:alice", "test/doc:d#b@auth/user:bob"])
    assert_index_matches(subjects=[user("alice"), user("bob")], resources=[doc()], actions=[name])
    assert_no_drift()
    original = active.schema()
    for length in (52, 64):
        active.set_schema(parse_zed(source.replace(name, "p" * length)))
        with pytest.raises(SchemaError, match=r"rebac\.E016.*64-character"):
            program_for(active, using="default")
    active.set_schema(original)
    assert_index_matches(subjects=[user("alice"), user("bob")], resources=[doc()], actions=[name])
    assert_no_drift()


def test_cached_atomic_program_preserves_maintained_index(install):
    from django.db import transaction

    active = install(BASE)
    with transaction.atomic():
        first = program_for(active, using="default")
        second = program_for(active, using="default")
        assert first.nodes is second.nodes
        seed(["test/doc:d#a@auth/user:alice", "test/doc:d#b@auth/user:bob"])
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc()],
        actions=["union", "intersection", "difference"],
    )
    assert_no_drift()


def test_projection_interns_only_region_userset_objects(install, monkeypatch):
    from rebac.index import project
    from rebac.models.index import IndexTerm, IndexWork

    active = install(BASE)
    seed(
        [
            "test/doc:d#a@test/group:near#member",
            "test/doc:other#a@test/group:far#member",
            "test/group:near#member@auth/user:alice",
            "test/group:far#member@auth/user:bob",
        ]
    )
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc(), doc("other")],
        actions=["a", "read"],
    )
    marker = IndexWork.objects.create(pass_id=0, kind="test", phase="new")
    near = IndexTerm.objects.get(type="test/group", object_id="near", relation="")
    IndexWork.objects.create(pass_id=marker.pk, kind="scope", phase="region", term=near)
    seen = []
    original = project._term

    def capture(source, type_, object_id, relation, using):
        if source.model is IndexTerm and getattr(relation, "value", None) == "member":
            seen.extend(source.values_list("object_id", flat=True))
        return original(source, type_, object_id, relation, using)

    with monkeypatch.context() as patcher:
        patcher.setattr(project, "_term", capture)
        project_edges(program_for(active, using="default"), using="default", region=marker.pk)
    assert seen == ["near"]
    IndexWork.objects.filter(pass_id=marker.pk).delete()
    marker.delete()
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc(), doc("other")],
        actions=["a", "read"],
    )
    assert_no_drift()


def test_region_repair_preserves_unrelated_intersection_scopes(install):
    from rebac.models.index import IndexTerm, IndexWork

    active = install("""
        caveat gate(enabled bool) { enabled }
        definition auth/user {}
        definition test/doc {
            relation approved: auth/user with gate
            permission read = authenticated & approved
        }
    """)
    active.write_relationships(
        [
            RelationshipTuple(doc(name), "approved", user("alice"), "gate")
            for name in ("d", "far-1", "far-2", "far-3")
        ]
    )
    selected = IndexTerm.objects.get(type="test/doc", object_id="d", relation="")
    universal = IndexTerm.objects.get(type="test/doc", object_id="*", relation="$type")
    marker = IndexWork.objects.create(pass_id=0, kind="test", phase="new")
    for term in (selected, universal):
        IndexWork.objects.create(pass_id=marker.pk, kind="scope", phase="region", term=term)
    IndexCover.objects.filter(scope=selected, node="read").delete()
    derive_nodes(program_for(active, using="default"), using="default", region=marker.pk)
    IndexWork.objects.filter(pass_id=marker.pk).delete()
    marker.delete()
    assert_index_matches(
        subjects=[user("alice")],
        resources=[doc(name) for name in ("d", "far-1", "far-2", "far-3")],
        actions=["read"],
        contexts=[None, {"enabled": True}, {"enabled": False}],
    )
    assert_no_drift()


def test_region_repair_preserves_unrelated_bans_on_type_level_cover(install):
    active = install("""
        definition auth/user {}
        definition test/doc {
            relation banned: auth/user
            permission read = authenticated - banned
            permission nested = read & authenticated
        }
    """)
    alice_ban = RelationshipTuple(doc("near"), "banned", user("alice"))
    bob_ban = RelationshipTuple(doc("far"), "banned", user("bob"))
    active.write_relationships([alice_ban, bob_ban])
    assert_no_drift()
    active.delete_relationship(alice_ban)
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc("near"), doc("far")],
        actions=["read", "nested"],
    )
    assert read.check(
        resource=doc("near"), action="read", actor=user("alice"), context=None, using="default"
    ).allowed
    assert not read.check(
        resource=doc("far"), action="read", actor=user("bob"), context=None, using="default"
    ).allowed
    assert IndexCover.objects.filter(node="read", site__gt="").exists()
    assert_no_drift()


@pytest.mark.pg_delta
@pytest.mark.parametrize("install", STORAGE_TIERS, indirect=True)
def test_batched_expiry_growth_for_recursive_covers_and_memberships(install, monkeypatch):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from rebac.index import project

    monkeypatch.setattr(project, "BATCH_SIZE", 2)
    active = install("""
        use expiration
        definition auth/user {}
        definition test/group {
            relation member: auth/user | test/group#member with expiration
        }
        definition test/doc {
            relation viewer: auth/user | test/group#member with expiration
            relation approved: auth/user
            relation parent: test/doc
            permission read = viewer + parent->read
            permission edit = read & approved
        }
    """)
    now = timezone.now()
    short, long = now + timedelta(minutes=1), now + timedelta(minutes=5)
    rows = [
        RelationshipTuple(
            ObjectRef("test/group", "g"), "member", SubjectRef.of("test/group", "sub", "member")
        ),
        RelationshipTuple(doc("group"), "viewer", SubjectRef.of("test/group", "g", "member")),
        RelationshipTuple(doc(), "parent", SubjectRef.of("test/doc", "leaf")),
    ]
    subjects = [user(f"u-{n}") for n in range(5)]
    for subject in subjects:
        rows.extend(
            [
                RelationshipTuple(
                    ObjectRef("test/group", "g"), "member", subject, expires_at=short
                ),
                RelationshipTuple(
                    ObjectRef("test/group", "sub"), "member", subject, expires_at=long
                ),
                RelationshipTuple(doc(), "viewer", subject, expires_at=short),
                RelationshipTuple(doc("leaf"), "viewer", subject, expires_at=long),
                RelationshipTuple(doc(), "approved", subject),
            ]
        )
    with CaptureQueriesContext(connection) as captured:
        active.write_relationships(rows)
    assert set(
        IndexMember.objects.filter(set__object_id="g", member__type="auth/user").values_list(
            "expires_at", flat=True
        )
    ) == {long}
    assert set(
        IndexCover.objects.filter(scope__object_id="d", node="read").values_list(
            "expires_at", flat=True
        )
    ) == {long}
    updates = [
        query["sql"]
        for query in captured
        if query["sql"].lstrip().upper().startswith("INSERT")
        and "UPDATE" in query["sql"].upper()
        and any(table in query["sql"] for table in ("rebac_grant", "rebac_membership"))
    ]
    assert updates
    # Batched upserts read no index table. (Django's PostgreSQL bulk_create
    # spells its rows as ``SELECT * FROM UNNEST(...)``; that is not a read.)
    assert all(not re.search(r'FROM\s+"rebac_', sql) for sql in updates)
    for instant in (now, short, long):
        assert_index_matches(
            subjects=subjects,
            resources=[doc(), doc("group"), doc("leaf")],
            actions=["read", "edit"],
            now=instant,
        )
    assert_no_drift()


def test_expiring_exclusion_then_intersection_general_lane(install):
    active = install("""
        use expiration
        definition auth/user {}
        definition test/doc {
            relation a: auth/user
            relation b: auth/user with expiration
            relation c: auth/user
            permission read = (a - b) & c
        }
    """)
    now = timezone.now()
    deadline = now + timedelta(minutes=10)
    seed(["test/doc:d#a@auth/user:alice", "test/doc:d#c@auth/user:alice"])
    active.write_relationships([RelationshipTuple(doc(), "b", user("alice"), expires_at=deadline)])
    rebuild(using="default")
    for instant in (
        now,
        deadline - timedelta(microseconds=1),
        deadline,
        deadline + timedelta(minutes=1),
    ):
        assert_index_matches(
            subjects=[user("alice"), user("bob")], resources=[doc()], actions=["read"], now=instant
        )
    assert IndexCover.objects.filter(node="read", site__gt="").exists()


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_timed_tighten_updates_dependents_and_arrows(settings, storage):
    from unittest.mock import patch

    from django.contrib.contenttypes.models import ContentType

    from rebac.models import SchemaDefinition, SchemaOverride, SchemaPermission, SchemaRelation
    from rebac.models.index import IndexState
    from rebac.models.schema_write import schema_index_write

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    IndexState.objects.get_or_create(key="global")
    with schema_index_write("default"):
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="test/doc")
        SchemaRelation.objects.create(
            definition=definition, name="a", allowed_subjects=[{"type": "auth/user"}]
        )
        SchemaRelation.objects.create(
            definition=definition, name="parent", allowed_subjects=[{"type": "test/doc"}]
        )
        permission = SchemaPermission.objects.create(
            definition=definition, name="p", expression="a"
        )
        SchemaPermission.objects.create(definition=definition, name="q", expression="p")
        SchemaPermission.objects.create(
            definition=definition, name="read", expression="q + parent->q"
        )
    seed(["test/doc:root#a@auth/user:alice", "test/doc:child#parent@test/doc:root"])
    deadline = timezone.now() + timedelta(minutes=5)
    SchemaOverride.objects.create(
        kind="tighten",
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=permission.pk,
        expression="nil",
        reason="timed tighten",
        expires_at=deadline,
    )
    assert IndexCover.objects.filter(site__gt="").exists()
    for instant in (deadline - timedelta(seconds=1), deadline):
        with (
            patch("django.utils.timezone.now", return_value=instant),
            patch("rebac.index.time.index_now", return_value=instant),
        ):
            assert_index_matches(
                subjects=[user("alice")],
                resources=[doc("root"), doc("child")],
                actions=["p", "q", "read"],
                now=instant,
            )


def test_wide_intersection_above_recursive_twelve_hop_chain(install):
    install("""
        definition auth/user {}
        definition test/tree {
            relation viewer: auth/user
            relation parent: test/tree
            permission p = viewer + parent->p
        }
        definition test/doc {
            relation a: test/tree
            relation b: test/tree
            relation c: test/tree
            relation d: test/tree
            permission read = (a->p + b->p) & (c->p + d->p)
        }
    """)
    edges = [f"test/tree:n{i}#parent@test/tree:n{i - 1}" for i in range(1, 13)]
    seed(
        [
            *edges,
            "test/tree:n0#viewer@auth/user:alice",
            "test/tree:other#viewer@auth/user:bob",
            "test/doc:d#a@test/tree:n12",
            "test/doc:d#b@test/tree:other",
            "test/doc:d#c@test/tree:n12",
        ]
    )
    rebuild(using="default")
    # Deliberately lower the production depth setting after the build. Reads
    # must use the materialized index; the oracle still needs its own headroom.
    with override_settings(REBAC_DEPTH_LIMIT=1):
        assert read.check(
            resource=doc(), action="read", actor=user("alice"), context=None, using="default"
        ).allowed
    assert_index_matches(
        subjects=[user("alice"), user("bob"), user("outsider")], resources=[doc()], actions=["read"]
    )


def test_field_and_filtered_constant_projection(install):
    from tests.testapp.models import Folder, Post

    install("""
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            permission read = viewer
        }
        definition blog/post {
            relation parent: blog/folder // rebac:field=folder
            relation fixed: auth/user // rebac:const={"target_id":"alice","filters":{"title":"public"}}
            permission read = parent->read + fixed
        }
    """)
    with sudo(reason="index projection test fixtures"):
        folder = Folder.objects.create(name="folder")
        public = Post.objects.create(title="public", folder=folder)
        private = Post.objects.create(title="private")
    seed([f"blog/folder:{folder.pk}#viewer@auth/user:bob"])
    rebuild(using="default")
    assert_index_matches(
        subjects=[user("alice"), user("bob"), user("unknown")],
        resources=[ObjectRef("blog/post", str(row.pk)) for row in (public, private)],
        actions=["read", "fixed", "parent"],
    )


def test_dynamic_and_fixed_attributes_preserve_tuple_precedence(install):
    from django.contrib.auth import get_user_model

    from rebac.models import active_relationship_model

    install("""
        definition auth/user {}
        definition test/fixed {
            relation member: auth/user // rebac:attribute={"field":"is_staff","resource":"staff","value":true}
            permission read = member
        }
        definition test/dynamic {
            relation member: auth/user // rebac:attribute={"field":"username"}
            permission read = member
        }
    """)
    staff = get_user_model().objects.create(username="alice", is_staff=True)
    other = get_user_model().objects.create(username="bob", is_staff=False)
    # Simulate persisted tuples predating the backing declaration. Projection
    # must suppress only the fixed anchor, and every dynamic container.
    model = active_relationship_model()
    for type_, resource in [
        ("test/fixed", "staff"),
        ("test/fixed", "elsewhere"),
        ("test/dynamic", "alice"),
    ]:
        model.objects.create(
            resource_type=type_,
            resource_id=resource,
            relation="member",
            subject_type="auth/user",
            subject_id=str(other.pk),
        )
    rebuild(using="default")
    assert_index_matches(
        subjects=[user(str(staff.pk)), user(str(other.pk))],
        resources=[
            ObjectRef("test/fixed", "staff"),
            ObjectRef("test/fixed", "elsewhere"),
            ObjectRef("test/dynamic", "alice"),
            ObjectRef("test/dynamic", "bob"),
        ],
        actions=["member", "read"],
    )


def test_projection_keeps_maximum_expiry_across_conflicts(install):
    active = install("""
        use expiration
        caveat ca(ok bool) { ok }
        caveat cb(ok bool) { ok }
        definition auth/user {}
        definition test/doc {
            relation a: auth/user with ca | auth/user with cb with expiration
            permission read = a
        }
    """)
    now = timezone.now()
    short, long = now + timedelta(minutes=1), now + timedelta(minutes=5)
    active.write_relationships(
        [
            RelationshipTuple(doc(), "a", user("alice"), "ca", {"ok": True}, short),
            RelationshipTuple(doc(), "a", user("alice"), "cb", {"ok": True}, long),
            RelationshipTuple(doc(), "a", user("bob"), "ca", {}, short),
            RelationshipTuple(doc(), "a", user("bob"), "cb", {}, long),
        ]
    )
    rebuild(using="default")

    def edges(name):
        rows = IndexEdge.objects.filter(
            resource__type="test/doc",
            resource__object_id="d",
            relation="a",
            subject__object_id=name,
        )
        return {
            (expiry, bool(key)) for expiry, key in rows.values_list("expires_at", "condition_key")
        }

    # Alice's caveats are decided by their pinned context: the two tuples are
    # one unconditional edge, which keeps the later expiry.
    assert edges("alice") == {(long, False)}
    # Bob's need runtime context: each alternative keeps its own expiry.
    assert edges("bob") == {(short, True), (long, True)}
    for instant in (now, short, long):
        assert_index_matches(
            subjects=[user("alice"), user("bob")],
            resources=[doc()],
            actions=["a", "read"],
            contexts=[None, {"ok": True}, {"ok": False}],
            now=instant,
        )


def test_pinned_declared_context_does_not_drop_missing_runtime_global(install):
    active = install("""
        caveat runtime(ok bool) { ok && runtime_flag }
        definition auth/user {}
        definition test/doc {
            relation blocked: auth/user with runtime
            permission read = authenticated - blocked
        }
    """)
    active.write_relationships(
        [RelationshipTuple(doc(), "blocked", user("alice"), "runtime", {"ok": True})]
    )
    rebuild(using="default")
    assert IndexEdge.objects.filter(condition_key__gt="").exists()
    assert_index_matches(
        subjects=[user("alice")],
        resources=[doc()],
        actions=["read"],
        contexts=[None, {"runtime_flag": True}, {"runtime_flag": False}],
    )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_timed_recaveat_keeps_pinned_leaf_in_projection(settings, storage):
    from unittest.mock import patch

    from django.contrib.contenttypes.models import ContentType

    from rebac.models import (
        SchemaCaveat,
        SchemaDefinition,
        SchemaOverride,
        SchemaPermission,
        SchemaRelation,
    )
    from rebac.models.index import IndexState
    from rebac.models.schema_write import schema_index_write

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    IndexState.objects.get_or_create(key="global")
    with schema_index_write("default"):
        caveat = SchemaCaveat.objects.create(
            name="gate", params=[{"name": "ok", "type": "bool"}], expression="ok"
        )
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="test/doc")
        SchemaRelation.objects.create(
            definition=definition,
            name="a",
            allowed_subjects=[{"type": "auth/user", "with_caveat": "gate"}],
        )
        SchemaPermission.objects.create(definition=definition, name="read", expression="a")
    backend().write_relationships(
        [RelationshipTuple(doc(), "a", user("alice"), "gate", {"ok": True})]
    )
    deadline = timezone.now() + timedelta(minutes=1)
    SchemaOverride.objects.create(
        kind="recaveat",
        target_ct=ContentType.objects.get_for_model(SchemaCaveat),
        target_pk=caveat.pk,
        expression="!ok",
        reason="timed recaveat",
        expires_at=deadline,
    )
    assert IndexEdge.objects.get(resource__object_id="d", relation="a").condition == (
        conditions.leaf("gate", {"ok": True})
    )
    for instant in (deadline - timedelta(seconds=1), deadline):
        with (
            patch("django.utils.timezone.now", return_value=instant),
            patch("rebac.index.time.index_now", return_value=instant),
        ):
            assert_index_matches(
                subjects=[user("alice")], resources=[doc()], actions=["read"], now=instant
            )


def test_naive_datetime_projection_and_intervals(install, settings):
    settings.USE_TZ = False
    active = install("""
        use expiration
        definition auth/user {}
        definition test/doc { relation a: auth/user with expiration
            permission read = a
        }
    """)
    now = timezone.now()
    deadline = now + timedelta(minutes=1)
    active.write_relationships([RelationshipTuple(doc(), "a", user("alice"), expires_at=deadline)])
    rebuild(using="default")
    for instant in (now, deadline):
        assert_index_matches(
            subjects=[user("alice")], resources=[doc()], actions=["read"], now=instant
        )


def test_projection_checks_exact_id_caveat_alternatives_and_expiration(install):
    from rebac.models import active_relationship_model

    install("""
        caveat ca(ok bool) { ok }
        caveat cb(ok bool) { ok }
        definition auth/user {}
        definition test/doc {
            relation a: auth/user:alice with ca | auth/user:bob with cb | auth/user:carol
            permission read = a
        }
    """)
    seed(["test/doc:d#a@auth/user:carol"])
    rows = active_relationship_model().objects
    for subject, caveat, expiration in [
        ("alice", "ca", timezone.now() + timedelta(days=1)),
        ("alice", "cb", None),
        ("bob", "ca", None),
    ]:
        rows.create(
            resource_type="test/doc",
            resource_id="d",
            relation="a",
            subject_type="auth/user",
            subject_id=subject,
            caveat_name=caveat,
            caveat_context={"ok": True},
            expires_at=expiration,
        )
    rebuild(using="default")
    assert_index_matches(
        subjects=[user(name) for name in ("alice", "bob", "carol")],
        resources=[doc()],
        actions=["a", "read"],
    )
    assert set(
        IndexEdge.objects.filter(resource_type="test/doc", relation="a").values_list(
            "subject__object_id", flat=True
        )
    ) == {"carol"}


def test_multihop_backing_filters_keep_the_same_child_join(install):
    from tests.testapp.models import Folder, Post

    install("""
        definition auth/user {}
        definition blog/post { relation viewer: auth/user
            permission read = viewer
        }
        definition blog/folder {
            relation pages: blog/post // rebac:field={"path":"children__posts","filters":{"children__is_active":true}}
            permission read = pages->read
        }
    """)
    with sudo(reason="multi-hop projection fixtures"):
        root = Folder.objects.create(name="root")
        active = Folder.objects.create(name="active", parent=root, is_active=True)
        inactive = Folder.objects.create(name="inactive", parent=root, is_active=False)
        visible = Post.objects.create(title="visible", folder=active)
        hidden = Post.objects.create(title="hidden", folder=inactive)
    seed(
        [
            f"blog/post:{visible.pk}#viewer@auth/user:bob",
            f"blog/post:{hidden.pk}#viewer@auth/user:alice",
        ]
    )
    rebuild(using="default")
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[ObjectRef("blog/folder", str(root.pk))],
        actions=["read"],
    )


def test_region_rederivation_leaves_unaffected_covers_untouched(install):
    from rebac.models.index import IndexTerm, IndexWork

    active = install("""
        definition auth/user {}
        definition test/doc { relation a: auth/user
            permission read = a
        }
    """)
    seed(["test/doc:d#a@auth/user:alice", "test/doc:untouched#a@auth/user:bob"])
    rebuild(using="default")
    witness = list(IndexCover.objects.filter(scope__object_id="untouched").order_by("pk").values())
    marker = IndexWork.objects.create(pass_id=0, kind="test", phase="region")
    terms = IndexTerm.objects.filter(type="test/doc", object_id="d")
    IndexWork.objects.bulk_create(
        [IndexWork(pass_id=marker.pk, phase="region", kind="scope", term=term) for term in terms]
    )
    IndexCover.objects.filter(scope__in=terms).delete()
    IndexMember.objects.filter(set__in=terms).delete()
    IndexEdge.objects.filter(resource__in=terms).delete()
    program = program_for(active, using="default")
    for operation in (project_edges, derive_memberships, derive_nodes):
        operation(program, using="default", region=marker.pk)
    assert (
        list(IndexCover.objects.filter(scope__object_id="untouched").order_by("pk").values())
        == witness
    )
    assert_index_matches(
        subjects=[user("alice"), user("bob")], resources=[doc(), doc("untouched")], actions=["read"]
    )
    IndexWork.objects.filter(pass_id=marker.pk).delete()
    marker.delete()


# Cases from the semantic review of the index design, against the index.

CONSTANT_ARROW = """
    caveat gate(a bool) { a }
    definition auth/user {}
    definition auth/anonymous {}
    definition test/org {
        relation member: auth/user
        relation banned: auth/user | auth/user with gate
        permission manage = authenticated - banned
        permission staff = member - banned
    }
    definition blog/post {
        relation org: test/org // rebac:const=default
        permission read = org->manage
        permission edit = org->staff
    }
"""
# A constant relation needs a resource type that maps to a model.
POSTS = [ObjectRef("blog/post", "one"), ObjectRef("blog/post", "unseen")]


def _allowed(resource, action, actor, context=None):
    return read.check(
        resource=resource, action=action, actor=actor, context=context, using="default"
    )


def test_site_behind_a_constant_arrow_is_evaluated_at_the_target(install):
    install(CONSTANT_ARROW)
    seed(
        [
            "test/org:default#banned@auth/user:alice",
            "test/org:default#member@auth/user:alice",
            "test/org:default#member@auth/user:bob",
        ]
    )
    resources = POSTS
    subjects = [user("alice"), user("bob"), user("carol")]
    assert_index_matches(subjects=subjects, resources=resources, actions=["read", "edit"])
    expected = {
        "read": {"alice": False, "bob": True, "carol": True},
        "edit": {"alice": False, "bob": True, "carol": False},
    }
    for resource in resources:
        for action, names in expected.items():
            for name, allowed in names.items():
                assert _allowed(resource, action, user(name)).allowed is allowed, (action, name)
    assert_no_drift()


def test_conditional_ban_behind_a_constant_arrow_is_evaluated_at_the_target(install):
    active = install(CONSTANT_ARROW)
    active.write_relationships(
        [
            RelationshipTuple(
                ObjectRef("test/org", "default"), "banned", user("alice"), "gate", {}
            ),
            RelationshipTuple(ObjectRef("test/org", "default"), "member", user("alice")),
        ]
    )
    resources = POSTS
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=resources,
        actions=["read", "edit"],
        contexts=[None, {"a": True}, {"a": False}],
    )
    for resource in resources:
        for action in ("read", "edit"):
            assert _allowed(resource, action, user("alice")).conditional_on == ("a",)
            assert not _allowed(resource, action, user("alice"), {"a": True}).allowed
            assert _allowed(resource, action, user("alice"), {"a": False}).allowed
    assert_no_drift()


def test_type_level_site_behind_an_arrow_is_evaluated_at_the_target(install):
    install("""
        definition auth/user {}
        definition auth/anonymous {}
        definition test/doc {
            relation r2: auth/user
            relation parent: test/doc
            permission p = authenticated - r2
            permission read = parent->p
        }
    """)
    seed(["test/doc:one#parent@test/doc:two", "test/doc:two#r2@auth/user:alice"])
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one"), doc("two")],
        actions=["p", "read"],
    )
    assert not _allowed(doc("one"), "read", user("alice")).allowed
    assert _allowed(doc("one"), "read", user("bob")).allowed
    assert_no_drift()


@pytest.mark.parametrize("tuples", [[], ["test/doc:one#r1@auth/user:alice"]])
def test_model_level_check_of_a_type_level_site_ignores_concrete_bans(install, tuples):
    install("""
        definition auth/user {}
        definition auth/anonymous {}
        definition test/doc {
            relation r1: auth/user
            permission read = authenticated - r1
        }
    """)
    seed(tuples)
    assert_index_matches(
        subjects=[user("alice"), user("bob")], resources=[doc(""), doc("one")], actions=["read"]
    )
    assert _allowed(doc(""), "read", user("alice")).allowed


def test_subject_enumeration_lists_named_subjects_and_nobody_by_class(install):
    install("""
        definition auth/user {}
        definition auth/anonymous {}
        definition test/group { relation member: auth/user | test/group#member }
        definition test/doc {
            relation r1: auth/user | test/group#member
            relation r2: auth/user
            relation parent: test/doc
            permission minus = r1 - r2
            permission both = r1 & (authenticated - r2)
            permission swapped = (authenticated - r2) & r1
            permission public = authenticated - r2
            permission either = authenticated + r1
            permission inherited = parent->minus
        }
    """)
    seed(
        [
            "test/doc:one#r1@test/group:g#member",
            "test/group:g#member@auth/user:alice",
            "test/group:g#member@auth/user:bob",
            "test/group:g#member@test/group:h#member",
            "test/group:h#member@auth/user:carol",
            "test/doc:one#r2@auth/user:alice",
            "test/doc:two#r1@auth/user:dave",
            "test/doc:child#parent@test/doc:one",
        ]
    )
    actions = ["r1", "minus", "both", "swapped", "public", "either", "inherited"]
    resources = [doc("one"), doc("two"), doc("child"), doc("unseen")]
    from tests.index_harness import assert_subjects_match

    assert_subjects_match(
        resources=resources, actions=actions, subject_types=["auth/user", "test/group"]
    )

    def listed(action, resource="one"):
        return {
            subject.subject_id
            for subject in read.lookup_subjects(
                resource=doc(resource), action=action, subject_type="auth/user", using="default"
            )
        }

    assert listed("minus") == listed("both") == listed("swapped") == {"bob", "carol"}
    assert listed("inherited", "child") == {"bob", "carol"}
    assert listed("public") == set()
    assert listed("either") == {"alice", "bob", "carol"}
    assert listed("either", "two") == {"dave"}


def _stored_schema(settings, storage, relations, permissions):
    """Install a schema through the policy write owners, so overrides apply."""
    from rebac.models import SchemaDefinition, SchemaPermission, SchemaRelation
    from rebac.models.index import IndexState
    from rebac.models.schema_write import schema_index_write

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    IndexState.objects.get_or_create(key="global")
    with schema_index_write("default"):
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="test/doc")
        for name, subject in relations.items():
            SchemaRelation.objects.create(
                definition=definition, name=name, allowed_subjects=[{"type": subject}]
            )
        return {
            name: SchemaPermission.objects.create(
                definition=definition, name=name, expression=expression
            )
            for name, expression in permissions.items()
        }


def _override(permission, kind, expression, **fields):
    from django.contrib.contenttypes.models import ContentType

    from rebac.models import SchemaOverride, SchemaPermission

    return SchemaOverride.objects.create(
        kind=kind,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=permission.pk,
        expression=expression,
        reason="test",
        **fields,
    )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_timed_tighten_to_nil_denies_until_its_deadline(settings, storage):
    from unittest.mock import patch

    permissions = _stored_schema(settings, storage, {"r1": "auth/user"}, {"read": "r1"})
    seed(["test/doc:one#r1@auth/user:alice"])
    deadline = timezone.now() + timedelta(minutes=5)
    _override(permissions["read"], "tighten", "nil", expires_at=deadline)
    for instant, allowed in ((deadline - timedelta(seconds=1), False), (deadline, True)):
        with (
            patch("django.utils.timezone.now", return_value=instant),
            patch("rebac.index.time.index_now", return_value=instant),
        ):
            assert_index_matches(
                subjects=[user("alice")], resources=[doc("one")], actions=["read"], now=instant
            )
            assert _allowed(doc("one"), "read", user("alice")).allowed is allowed


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_override_that_the_index_refuses_is_rolled_back_when_written(settings, storage):
    from rebac.models import SchemaOverride
    from rebac.models.generation import SchemaGeneration

    permissions = _stored_schema(
        settings,
        storage,
        {"r1": "auth/user", "r2": "auth/user", "parent": "test/doc"},
        {"read": "r1 + parent->read", "view": "r1"},
    )
    seed(["test/doc:one#r1@auth/user:alice", "test/doc:two#parent@test/doc:one"])
    witness = SchemaGeneration.objects.witness("default")
    rows = list(IndexCover.objects.order_by("pk").values())

    def unchanged():
        assert not SchemaOverride.objects.exists()
        assert SchemaGeneration.objects.witness("default") == witness
        assert list(IndexCover.objects.order_by("pk").values()) == rows
        assert_index_matches(
            subjects=[user("alice"), user("bob")],
            resources=[doc("one"), doc("two")],
            actions=["read", "view"],
        )

    # A tighten puts a site on the cycle of a recursive permission.
    with pytest.raises(SchemaError, match=r"rebac\.E016.*tighten"):
        _override(permissions["read"], "tighten", "r2")
    unchanged()
    # A disable makes the plan of `view` three lookups.
    with override_settings(REBAC_INDEX_LOOKUP_LIMIT=1):
        with pytest.raises(SchemaError, match=r"rebac\.E019"):
            _override(permissions["view"], "disable", "r2")
        reset_backend()
    unchanged()
    _override(permissions["view"], "disable", "r2")
    assert_index_matches(subjects=[user("alice")], resources=[doc("one")], actions=["read", "view"])
    assert_no_drift()


def test_userset_relation_used_as_a_site_operand_has_rows(install):
    install("""
        definition auth/user {}
        definition test/group {
            relation member: auth/user | test/group#member
            relation banned: auth/user
            permission active = member - banned
        }
    """)
    seed(
        [
            "test/group:g#member@auth/user:alice",
            "test/group:g#member@auth/user:bob",
            "test/group:g#member@test/group:h#member",
            "test/group:h#member@auth/user:carol",
            "test/group:g#banned@auth/user:alice",
        ]
    )
    group = ObjectRef("test/group", "g")
    assert_index_matches(
        subjects=[user(name) for name in ("alice", "bob", "carol", "dave")],
        resources=[group, ObjectRef("test/group", "h")],
        actions=["member", "banned", "active"],
    )
    expected = {
        "active": {"alice": False, "bob": True, "carol": True, "dave": False},
        "member": {"alice": True, "bob": True, "carol": True, "dave": False},
    }
    for action, names in expected.items():
        for name, allowed in names.items():
            assert _allowed(group, action, user(name)).allowed is allowed, (action, name)
    assert_no_drift()


PINNED = """
    caveat ca(a bool) { a }
    definition auth/user {}
    definition auth/anonymous {}
    definition test/doc {
        relation r1: auth/user | auth/user with ca
        permission granted = r1
        permission public = authenticated - r1
    }
"""


@pytest.mark.parametrize("pinned,action", [(True, "granted"), (False, "public")])
def test_fully_pinned_caveat_is_decided_at_derivation(install, pinned, action):
    active = install(PINNED)
    active.write_relationships(
        [RelationshipTuple(doc("one"), "r1", user("alice"), "ca", {"a": pinned})]
    )
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one")],
        actions=["r1", "granted", "public"],
        contexts=[None, {"a": True}, {"a": False}],
    )
    assert _allowed(doc("one"), action, user("alice")) == read.CheckResult.has()
    listed = read.accessible_ids(
        resource_type="test/doc", action=action, actor=user("alice"), using="default"
    )
    assert list(listed) == ["one"]
    assert not IndexEdge.objects.exclude(condition_key="").exists()
    assert_no_drift()


MISSING = """
    use expiration
    caveat gate(a bool, b bool) { a && b }
    caveat ca(a bool) { a }
    caveat cb(b bool) { b }
    definition auth/user {}
    definition auth/anonymous {}
    definition test/group { relation member: auth/user | auth/user with gate | auth/user with cb }
    definition test/doc {
        relation r1: auth/user with ca | test/group#member with gate | test/group#member with ca
        relation r2: auth/user | auth/user with cb
        relation r3: test/group#member
        relation parent: test/doc | test/doc with gate
        permission left = (r1 + r2) - parent->r2
        permission right = (r2 + r1) - parent->r2
        permission group = r1 + r2
        permission arrow = parent->r1 + r2
        permission p = r1 - r2
        permission under = authenticated - parent->p
        permission banned = authenticated - r3
    }
"""
CONTEXTS = [None, {"a": True}, {"b": True}, {"a": True, "b": True}, {"a": False, "b": True}]


def _write(active, *rows):
    active.write_relationships(
        [
            RelationshipTuple(resource, relation, subject, caveat, pinned or {})
            for resource, relation, subject, caveat, pinned in rows
        ]
    )


def _group(name="g"):
    return SubjectRef.of("test/group", name, "member")


@pytest.mark.parametrize("action", ["left", "right"])
def test_missing_parameters_do_not_depend_on_the_order_of_arms(install, action):
    _write(
        install(MISSING),
        (doc("one"), "r1", user("alice"), "ca", None),
        (doc("one"), "r2", user("alice"), "", None),
        (doc("one"), "parent", SubjectRef(doc("two")), "", None),
        (doc("two"), "r2", user("alice"), "cb", None),
    )
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one"), doc("two")],
        actions=["left", "right"],
        contexts=CONTEXTS,
    )
    assert _allowed(doc("one"), action, user("alice")).conditional_on == ("b",)
    assert_no_drift()


@pytest.mark.parametrize("action", ["group", "arrow"])
def test_a_path_that_cannot_hold_needs_no_parameter(install, action):
    _write(
        install(MISSING),
        (doc("one"), "r1", _group(), "gate", {"b": True}),
        (ObjectRef("test/group", "g"), "member", user("bob"), "", None),
        (doc("one"), "parent", SubjectRef(doc("two")), "gate", {"b": True}),
        (doc("one"), "r2", user("alice"), "cb", None),
    )
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one")],
        actions=["group", "arrow"],
        contexts=CONTEXTS,
    )
    assert _allowed(doc("one"), action, user("alice")).conditional_on == ("b",)
    assert_no_drift()


def test_conditional_arrow_into_a_site_under_an_exclusion(install):
    _write(
        install(MISSING),
        (doc("one"), "parent", SubjectRef(doc("two")), "gate", None),
        (doc("two"), "r1", user("alice"), "ca", {"a": True}),
    )
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one"), doc("two")],
        actions=["p", "under"],
        contexts=CONTEXTS,
    )
    alice = user("alice")
    assert _allowed(doc("one"), "under", alice).conditional_on == ("a", "b")
    assert not _allowed(doc("one"), "under", alice, {"a": True, "b": True}).allowed
    assert _allowed(doc("one"), "under", alice, {"a": False, "b": True}).allowed
    assert _allowed(doc("one"), "under", user("bob")).allowed
    listed = read.accessible_ids(
        resource_type="test/doc", action="under", actor=alice, using="default"
    )
    assert list(listed) == ["two"]
    assert_no_drift()


def test_caveat_on_a_membership_makes_an_uncaveated_relation_conditional(install):
    _write(
        install(MISSING),
        (doc("one"), "r3", _group(), "", None),
        (ObjectRef("test/group", "g"), "member", user("alice"), "gate", None),
    )
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one")],
        actions=["r3", "banned"],
        contexts=CONTEXTS,
    )
    assert _allowed(doc("one"), "banned", user("alice")).conditional_on == ("a", "b")
    assert _allowed(doc("one"), "banned", user("bob")).allowed
    assert_no_drift()


def test_an_alternative_that_holds_makes_the_other_one_unneeded(install):
    _write(
        install(MISSING),
        (doc("one"), "r1", _group(), "ca", None),
        (ObjectRef("test/group", "g"), "member", user("alice"), "", None),
        (ObjectRef("test/group", "g"), "member", user("alice"), "cb", None),
    )
    assert_index_matches(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one")],
        actions=["r1", "group"],
        contexts=CONTEXTS,
    )
    assert _allowed(doc("one"), "r1", user("alice")).conditional_on == ("a",)
    assert _allowed(doc("one"), "r1", user("alice"), {"a": True}).allowed
    assert_no_drift()


ANCHOR = """
    definition auth/user {}
    definition test/roleanchor { relation member: auth/user | test/roleanchor#member }
    definition test/doc {
        relation role: test/roleanchor
        relation grants: test/roleanchor#member
        permission read = role->member
        permission view = grants
    }
"""


def test_resource_type_whose_model_has_no_table(install):
    from tests.index_harness import assert_subjects_match
    from tests.testapp.models import RoleAnchor

    assert not RoleAnchor._meta.managed
    active = install(ANCHOR)
    seed(
        [
            "test/roleanchor:admin#member@auth/user:alice",
            "test/roleanchor:staff#member@test/roleanchor:admin#member",
            "test/doc:one#role@test/roleanchor:staff",
            "test/doc:one#grants@test/roleanchor:staff#member",
        ]
    )
    anchors = [ObjectRef("test/roleanchor", name) for name in ("admin", "staff", "unseen")]
    for types in (None, ["test/roleanchor"], ["test/doc"]):
        rebuild(using="default", types=types)
        assert_index_matches(
            subjects=[user("alice"), user("bob")],
            resources=[doc("one"), *anchors],
            actions=["read", "view", "member"],
        )
        assert_subjects_match(
            resources=[doc("one"), *anchors],
            actions=["read", "view", "member"],
            subject_types=["auth/user"],
        )
    assert _allowed(doc("one"), "read", user("alice")).allowed
    assert set(
        active.accessible(subject=user("alice"), action="member", resource_type="test/roleanchor")
    ) == {"admin", "staff"}
    active.delete_relationships(
        RelationshipFilter(resource_type="test/roleanchor", resource_id="admin")
    )
    assert not _allowed(doc("one"), "read", user("alice")).allowed
    assert_no_drift()


def test_unresolvable_backing_names_the_missing_model(install):
    # No Django model declares test/unmodelled, so its field backing cannot
    # resolve; the index build says why, as rebac.E009 does.
    with pytest.raises(
        SchemaError,
        match=r"test/unmodelled#owner: field-backed relation requires a concrete Django model "
        r"for resource type test/unmodelled \(rebac\.E009\)",
    ):
        install("""
            definition auth/user {}
            definition test/unmodelled {
                relation owner: auth/user // rebac:field=author
                permission read = owner
            }
        """)
