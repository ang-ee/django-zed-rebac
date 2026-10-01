"""Permission semantics against source facts, the reference model, and the walker."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from rebac import (
    ObjectRef,
    PermissionDepthExceeded,
    RelationshipTuple,
    SchemaError,
    SubjectRef,
    backend,
    sudo,
)
from rebac.backends import reset_backend
from rebac.compile import formulas
from rebac.schema import parse_zed
from rebac.testing import install_schema
from rebac.types import CheckResult, RelationshipFilter
from tests.backend_setup import STORAGE_TIERS
from tests.reference_harness import (
    assert_reads_match,
    assert_scope_matches,
    assert_subjects_match,
    seed,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(params=("denormalized", "registry"))
def install(request, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    return install_schema


def user(name):
    return SubjectRef.of("auth/user", name)


def doc(name="d"):
    return ObjectRef("test/doc", name)


def _check(resource, action, actor, context=None):
    return backend().check_access(subject=actor, action=action, resource=resource, context=context)


def _accessible(action, actor, resource_type="test/doc"):
    return list(backend().accessible(subject=actor, action=action, resource_type=resource_type))


def _scoped(model, action, actor):
    rows = model.objects.with_actor(actor).with_action(action)
    return set(rows.values_list("pk", flat=True))


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
def test_all_set_operations_and_finite_exclusion(install):
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
    assert_reads_match(
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
    assert not _check(doc(), "difference", user("alice")).allowed
    assert _check(doc(), "difference", user("bob")).allowed


@pytest.mark.parametrize(
    "left_group,right_group", [(False, False), (True, False), (False, True), (True, True)]
)
def test_intersection_of_direct_and_group_operands(install, left_group, right_group):
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
    assert_reads_match(subjects=subjects, resources=[doc()], actions=["edit"])
    assert _accessible("edit", user("alice")) == []
    assert _accessible("edit", user("bob")) == ["d"]


@pytest.mark.parametrize("conditional", ["right-cover", "left-member", "right-member"])
def test_intersection_with_a_caveated_operand_stays_conditional(install, conditional):
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
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc()],
        actions=["edit"],
        contexts=[None, {"enabled": False}, {"enabled": True}],
    )
    assert _check(doc(), "edit", user("bob")).conditional_on == ("enabled",)
    assert _accessible("edit", user("bob")) == []


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
    assert_reads_match(
        subjects=[user("alice")],
        resources=resources,
        actions=["read"],
        contexts=[None, {"enabled": False}, {"enabled": True}],
    )
    assert_scope_matches(Post, actor=user("alice"), action="read")
    assert _scoped(Post, "read", user("alice")) == {plain.pk}
    assert _check(resources[1], "read", user("alice")).conditional_on == ("enabled",)


def test_nested_conditional_ban_reaches_the_exclusion(install):
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
    assert_reads_match(
        subjects=[user("alice"), user("bob"), user("carol")],
        resources=[resource],
        actions=["view", "banned"],
        contexts=[None, {"enabled": False}, {"enabled": True}],
    )
    bad = ObjectRef("test/group", "bad")
    assert _check(bad, "member", user("alice")).allowed
    assert _check(bad, "member", user("bob")).conditional_on == ("enabled",)
    assert_scope_matches(Post, actor=user("bob"), action="view")
    assert _scoped(Post, "view", user("bob")) == set()
    assert _check(resource, "view", user("bob")).conditional_on == ("enabled",)


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
    assert_reads_match(
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
    assert _check(doc(), "read", user("alice")).conditional_on == ("outer",)
    assert _check(doc(), "read", user("bob"), {"outer": True}).conditional_on == ("inner",)


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
    assert_reads_match(
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
    assert_reads_match(
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
    assert_reads_match(
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
    assert_reads_match(
        subjects=[
            SubjectRef.of("auth/anonymous", id_, suffix)
            for id_ in ("", "*", "other")
            for suffix in ("", "member")
        ],
        resources=[doc()],
        actions=["read", "remainder"],
    )


@pytest.mark.parametrize("conditional_path", ["tuple", "membership"])
def test_conditional_exclusion_hole_is_a_definite_regrant(install, conditional_path):
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
    assert_reads_match(
        subjects=[user("alice"), user("unknown")],
        resources=[doc()],
        actions=["read"],
        contexts=[None, {"enabled": False}, {"enabled": True}],
    )
    assert _check(doc(), "read", user("alice")).allowed


def test_membership_recursion_through_a_group_cycle(install):
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
    assert_reads_match(subjects=[user("alice")], resources=[doc()], actions=["a", "union"])
    assert _check(ObjectRef("test/group", "outer"), "member", user("alice")).allowed
    assert not _check(doc(), "a", user("outsider")).allowed


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
    assert_reads_match(
        subjects=[user("alice")],
        resources=[doc()],
        actions=["read"],
        contexts=[{"middle": False}, {"middle": True}, None],
    )


def test_wildcard_grant_with_a_ban_travels_through_recursive_data_cycle(install):
    install(BASE)
    seed(
        [
            "test/doc:root#a@auth/user:*",
            "test/doc:root#b@auth/user:blocked",
            "test/doc:root#parent@test/doc:child",
            "test/doc:child#parent@test/doc:root",
        ]
    )
    assert_reads_match(
        subjects=[user("allowed")], resources=[doc("root"), doc("child")], actions=["read"]
    )
    for resource in [doc("root"), doc("child")]:
        assert not _check(resource, "read", user("blocked")).allowed


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
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[ObjectRef("blog/post", value) for value in ("banned", "other", "nonexistent")],
        actions=["constant", "read"],
    )


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
    assert_reads_match(
        subjects=[user("alice"), user("bob"), SubjectRef.of("blog/post", "virtual", "member")],
        resources=[doc()],
        actions=["a", "read"],
    )
    assert _check(ObjectRef("blog/post", "virtual"), "member", user("alice")).allowed


def test_arrow_includes_type_level_target_grants_and_ignores_suffix(install):
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
    assert_reads_match(
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
    assert_reads_match(
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
    assert _check(doc("root"), "missing", user("alice")).conditional_on == ("x", "y")


def test_formula_no_absorption_or_embedded_expression():
    schema = parse_zed("caveat ca(x bool) { x } caveat cb(y bool) { y }")
    a, b = formulas.leaf("ca", {}), formulas.leaf("cb", {})
    formula = formulas.or_(a, formulas.and_(a, b))
    assert formulas.size(formula) == 3
    # The formula keeps both paths. What it needs is what its value depends
    # on: the second path holds only where the first one does.
    assert formulas.evaluate(formula, schema, None) == (None, frozenset({"x"}))
    assert formulas.evaluate(formulas.and_(a, b), schema, None) == (
        None,
        frozenset({"x", "y"}),
    )
    assert len(formulas.key(formula)) == 64
    assert formulas.key(formulas.leaf("ca", {"z": 1, "a": 2})) == formulas.key(
        formulas.leaf("ca", {"a": 2, "z": 1})
    )
    replaced = parse_zed("caveat ca(x bool) { !x } caveat cb(y bool) { y }")
    assert formulas.evaluate(a, schema, {"x": True})[0] is True
    assert formulas.evaluate(a, replaced, {"x": True})[0] is False


def test_union_of_caveated_relations_with_distinct_pinned_contexts(install):
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
    assert_reads_match(
        subjects=[user("alice")],
        resources=[doc()],
        actions=["read"],
        contexts=[None, {"flag": True}],
    )


def test_plain_and_caveated_alternatives_of_one_union(install):
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
    assert_reads_match(
        subjects=[user("alice"), user("bob"), user("carol"), user("dave")],
        resources=[doc()],
        actions=["read"],
        contexts=[None, {"enabled": False}, {"enabled": True}],
    )


def test_relations_not_referenced_as_usersets_are_directly_checkable(install):
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
    # BASE references doc#a as a userset, but neither b nor parent.
    assert_reads_match(
        subjects=[user("alice"), user("bob"), user("carol")],
        resources=[doc(), doc("other")],
        actions=["a", "b", "parent", "intersection", "read"],
    )


def test_long_permission_name(install):
    name = "p" * 51
    install(f"""
        definition auth/user {{}}
        definition test/doc {{ relation a: auth/user relation b: auth/user
            permission {name} = (a + b) & a
        }}
    """)
    seed(["test/doc:d#a@auth/user:alice", "test/doc:d#b@auth/user:bob"])
    assert_reads_match(subjects=[user("alice"), user("bob")], resources=[doc()], actions=[name])


def test_tuples_written_inside_a_transaction_are_read_after_it(install):
    from django.db import transaction

    install(BASE)
    with transaction.atomic():
        seed(["test/doc:d#a@auth/user:alice", "test/doc:d#b@auth/user:bob"])
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc()],
        actions=["union", "intersection", "difference"],
    )


def test_separate_groups_grant_separate_documents(install):
    install(BASE)
    seed(
        [
            "test/doc:d#a@test/group:near#member",
            "test/doc:other#a@test/group:far#member",
            "test/group:near#member@auth/user:alice",
            "test/group:far#member@auth/user:bob",
        ]
    )
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc(), doc("other")],
        actions=["a", "read"],
    )


def test_caveated_intersection_with_a_class_on_several_documents(install):
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
    assert_reads_match(
        subjects=[user("alice")],
        resources=[doc(name) for name in ("d", "far-1", "far-2", "far-3")],
        actions=["read"],
        contexts=[None, {"enabled": True}, {"enabled": False}],
    )


def test_removing_one_ban_leaves_unrelated_bans_in_force(install):
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
    active.delete_relationship(alice_ban)
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc("near"), doc("far")],
        actions=["read", "nested"],
    )
    assert _check(doc("near"), "read", user("alice")).allowed
    assert not _check(doc("far"), "read", user("bob")).allowed


@pytest.mark.pg_delta
@pytest.mark.parametrize("install", STORAGE_TIERS, indirect=True)
def test_expiry_of_recursive_grants_and_memberships(install):
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
    active.write_relationships(rows)
    for instant in (now, short, long):
        assert_reads_match(
            subjects=subjects,
            resources=[doc(), doc("group"), doc("leaf")],
            actions=["read", "edit"],
            now=instant,
        )
    # A grant lasts as long as its longest path: the nested group and the
    # parent document outlive the direct rows.
    for instant, allowed in ((short, True), (long, False)):
        with patch("django.utils.timezone.now", return_value=instant):
            for resource in (doc(), doc("group")):
                assert _check(resource, "read", subjects[0]).allowed is allowed


def test_expiring_exclusion_then_intersection(install):
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
    for instant in (
        now,
        deadline - timedelta(microseconds=1),
        deadline,
        deadline + timedelta(minutes=1),
    ):
        assert_reads_match(
            subjects=[user("alice"), user("bob")], resources=[doc()], actions=["read"], now=instant
        )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_timed_tighten_updates_dependents_and_arrows(settings, storage):
    from django.contrib.contenttypes.models import ContentType

    from rebac.models import SchemaDefinition, SchemaOverride, SchemaPermission, SchemaRelation
    from rebac.models.schema_write import schema_index_write

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
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
    for instant in (deadline - timedelta(seconds=1), deadline):
        with patch("django.utils.timezone.now", return_value=instant):
            assert_reads_match(
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
    alice, others = user("alice"), [user("bob"), user("outsider")]
    # Alice's only path is twelve hops long. Beyond the bound a point check
    # refuses to answer and a scope leaves the row out.
    with pytest.raises(PermissionDepthExceeded):
        _check(doc(), "read", alice)
    assert _accessible("read", alice) == []
    # Nothing on the chain grants the others: what they hold is complete
    # within the bound, so their denial is an answer.
    assert_reads_match(subjects=others, resources=[doc()], actions=["read"])
    with override_settings(REBAC_DEPTH_LIMIT=12):
        assert _check(doc(), "read", alice).allowed
        assert _accessible("read", alice) == ["d"]
        assert_reads_match(subjects=[alice, *others], resources=[doc()], actions=["read"])


def test_field_backed_arrow_and_filtered_constant(install):
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
    with sudo(reason="backing test fixtures"):
        folder = Folder.objects.create(name="folder")
        public = Post.objects.create(title="public", folder=folder)
        private = Post.objects.create(title="private")
    seed([f"blog/folder:{folder.pk}#viewer@auth/user:bob"])
    assert_reads_match(
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
    with sudo(reason="test.attribute-fixture"):
        staff = get_user_model().objects.create(username="alice", is_staff=True)
        other = get_user_model().objects.create(username="bob", is_staff=False)
    # Simulate persisted tuples predating the backing declaration. The backing
    # replaces the tuples of the fixed anchor only, and of every dynamic container.
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
    assert_reads_match(
        subjects=[user(str(staff.pk)), user(str(other.pk))],
        resources=[
            ObjectRef("test/fixed", "staff"),
            ObjectRef("test/fixed", "elsewhere"),
            ObjectRef("test/dynamic", "alice"),
            ObjectRef("test/dynamic", "bob"),
        ],
        actions=["member", "read"],
    )


def test_caveat_alternatives_keep_their_own_expiry(install):
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
    for instant in (now, short, long):
        assert_reads_match(
            subjects=[user("alice"), user("bob")],
            resources=[doc()],
            actions=["a", "read"],
            contexts=[None, {"ok": True}, {"ok": False}],
            now=instant,
        )
    # Alice's caveats are decided by their pinned context, so she holds the
    # relation until the later expiry. Bob's need runtime context: each
    # alternative stays conditional until its own expiry.
    with patch("django.utils.timezone.now", return_value=short):
        assert _check(doc(), "read", user("alice")) == CheckResult.has()
        assert _check(doc(), "read", user("bob")).conditional_on == ("ok",)
    with patch("django.utils.timezone.now", return_value=long):
        for name in ("alice", "bob"):
            assert _check(doc(), "read", user(name), {"ok": True}) == CheckResult.no()


def test_pinned_declared_context_does_not_drop_missing_runtime_global(install):
    active = install("""
        caveat runtime(ok bool, runtime_flag bool) { ok && runtime_flag }
        definition auth/user {}
        definition test/doc {
            relation blocked: auth/user with runtime
            permission read = authenticated - blocked
        }
    """)
    active.write_relationships(
        [RelationshipTuple(doc(), "blocked", user("alice"), "runtime", {"ok": True})]
    )
    assert_reads_match(
        subjects=[user("alice")],
        resources=[doc()],
        actions=["read"],
        contexts=[None, {"runtime_flag": True}, {"runtime_flag": False}],
    )
    assert _check(doc(), "read", user("alice")).conditional_on == ("runtime_flag",)


def test_undeclared_caveat_runtime_identifier_is_rejected():
    from rebac.schema.parser import validate_schema

    schema = parse_zed("caveat runtime(ok bool) { ok && runtime_flag }")
    assert any("undeclared identifier 'runtime_flag'" in error for error in validate_schema(schema))


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_timed_recaveat_applies_to_a_pinned_caveat_until_its_deadline(settings, storage):
    from django.contrib.contenttypes.models import ContentType

    from rebac.models import (
        SchemaCaveat,
        SchemaDefinition,
        SchemaOverride,
        SchemaPermission,
        SchemaRelation,
    )
    from rebac.models.schema_write import schema_index_write

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
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
    for instant, allowed in ((deadline - timedelta(seconds=1), False), (deadline, True)):
        with patch("django.utils.timezone.now", return_value=instant):
            assert_reads_match(
                subjects=[user("alice")], resources=[doc()], actions=["read"], now=instant
            )
            assert _check(doc(), "read", user("alice")).allowed is allowed


def test_naive_datetime_expiry_intervals(install, settings):
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
    for instant in (now, deadline):
        assert_reads_match(
            subjects=[user("alice")], resources=[doc()], actions=["read"], now=instant
        )


def test_stored_tuples_must_match_exact_id_caveat_and_expiration_alternatives(install):
    from rebac.models import active_relationship_model

    active = install("""
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
    assert_reads_match(
        subjects=[user(name) for name in ("alice", "bob", "carol")],
        resources=[doc()],
        actions=["a", "read"],
    )
    assert {
        subject.subject_id
        for subject in active.lookup_subjects(resource=doc(), action="a", subject_type="auth/user")
    } == {"carol"}


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
    with sudo(reason="multi-hop backing fixtures"):
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
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[ObjectRef("blog/folder", str(root.pk))],
        actions=["read"],
    )


def test_direct_grants_on_separate_documents(install):
    install("""
        definition auth/user {}
        definition test/doc { relation a: auth/user
            permission read = a
        }
    """)
    seed(["test/doc:d#a@auth/user:alice", "test/doc:untouched#a@auth/user:bob"])
    assert_reads_match(
        subjects=[user("alice"), user("bob")], resources=[doc(), doc("untouched")], actions=["read"]
    )


# Cases from the semantic review of exclusions, arrows and caveats.

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


def test_exclusion_behind_a_constant_arrow_is_evaluated_at_the_target(install):
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
    assert_reads_match(subjects=subjects, resources=resources, actions=["read", "edit"])
    expected = {
        "read": {"alice": False, "bob": True, "carol": True},
        "edit": {"alice": False, "bob": True, "carol": False},
    }
    for resource in resources:
        for action, names in expected.items():
            for name, allowed in names.items():
                assert _check(resource, action, user(name)).allowed is allowed, (action, name)


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
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=resources,
        actions=["read", "edit"],
        contexts=[None, {"a": True}, {"a": False}],
    )
    for resource in resources:
        for action in ("read", "edit"):
            assert _check(resource, action, user("alice")).conditional_on == ("a",)
            assert not _check(resource, action, user("alice"), {"a": True}).allowed
            assert _check(resource, action, user("alice"), {"a": False}).allowed


def test_type_level_exclusion_behind_an_arrow_is_evaluated_at_the_target(install):
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
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one"), doc("two")],
        actions=["p", "read"],
    )
    assert not _check(doc("one"), "read", user("alice")).allowed
    assert _check(doc("one"), "read", user("bob")).allowed


@pytest.mark.parametrize("tuples", [[], ["test/doc:one#r1@auth/user:alice"]])
def test_model_level_check_of_a_type_level_exclusion_ignores_concrete_bans(install, tuples):
    install("""
        definition auth/user {}
        definition auth/anonymous {}
        definition test/doc {
            relation r1: auth/user
            permission read = authenticated - r1
        }
    """)
    seed(tuples)
    assert_reads_match(
        subjects=[user("alice"), user("bob")], resources=[doc(""), doc("one")], actions=["read"]
    )
    assert _check(doc(""), "read", user("alice")).allowed


def test_subject_enumeration_lists_named_subjects_and_nobody_by_class(install):
    active = install("""
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
    assert_subjects_match(
        resources=resources, actions=actions, subject_types=["auth/user", "test/group"]
    )

    def listed(action, resource="one"):
        return {
            subject.subject_id
            for subject in active.lookup_subjects(
                resource=doc(resource), action=action, subject_type="auth/user"
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
    from rebac.models.schema_write import schema_index_write

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
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
    permissions = _stored_schema(settings, storage, {"r1": "auth/user"}, {"read": "r1"})
    seed(["test/doc:one#r1@auth/user:alice"])
    deadline = timezone.now() + timedelta(minutes=5)
    _override(permissions["read"], "tighten", "nil", expires_at=deadline)
    for instant, allowed in ((deadline - timedelta(seconds=1), False), (deadline, True)):
        with patch("django.utils.timezone.now", return_value=instant):
            assert_reads_match(
                subjects=[user("alice")], resources=[doc("one")], actions=["read"], now=instant
            )
            assert _check(doc("one"), "read", user("alice")).allowed is allowed


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_override_that_the_compiler_refuses_is_rolled_back_when_written(settings, storage):
    from rebac.models import SchemaOverride
    from rebac.models.generation import SchemaGeneration

    permissions = _stored_schema(
        settings,
        storage,
        {"r1": "auth/user", "r2": "auth/user", "parent": "test/doc"},
        {"read": "r1 + parent->read", "view": "r1"},
    )
    seed(["test/doc:one#r1@auth/user:alice", "test/doc:two#parent@test/doc:one"])
    revision = SchemaGeneration.objects.revision("default")

    def reads_match():
        assert_reads_match(
            subjects=[user("alice"), user("bob")],
            resources=[doc("one"), doc("two")],
            actions=["read", "view"],
        )

    # A disable puts an exclusion on the cycle of a recursive permission.
    with pytest.raises(SchemaError, match="exclusion dependency"):
        _override(permissions["read"], "disable", "parent->read")
    assert not SchemaOverride.objects.exists()
    assert SchemaGeneration.objects.revision("default") == revision
    reads_match()
    _override(permissions["view"], "disable", "r2")
    reads_match()
    # A tighten leaves the cycle linear and free of exclusions: it is accepted.
    seed(["test/doc:one#r2@auth/user:alice"])
    _override(permissions["read"], "tighten", "r2")
    reads_match()
    assert _check(doc("one"), "read", user("alice")).allowed
    assert not _check(doc("two"), "read", user("alice")).allowed


def test_userset_relation_used_as_an_exclusion_operand(install):
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
    assert_reads_match(
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
            assert _check(group, action, user(name)).allowed is allowed, (action, name)


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
def test_fully_pinned_caveat_is_decided_without_context(install, pinned, action):
    active = install(PINNED)
    active.write_relationships(
        [RelationshipTuple(doc("one"), "r1", user("alice"), "ca", {"a": pinned})]
    )
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one")],
        actions=["r1", "granted", "public"],
        contexts=[None, {"a": True}, {"a": False}],
    )
    assert _check(doc("one"), action, user("alice")) == CheckResult.has()
    assert _accessible(action, user("alice")) == ["one"]


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
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one"), doc("two")],
        actions=["left", "right"],
        contexts=CONTEXTS,
    )
    assert _check(doc("one"), action, user("alice")).conditional_on == ("b",)


@pytest.mark.parametrize("action", ["group", "arrow"])
def test_a_path_that_cannot_hold_needs_no_parameter(install, action):
    _write(
        install(MISSING),
        (doc("one"), "r1", _group(), "gate", {"b": True}),
        (ObjectRef("test/group", "g"), "member", user("bob"), "", None),
        (doc("one"), "parent", SubjectRef(doc("two")), "gate", {"b": True}),
        (doc("one"), "r2", user("alice"), "cb", None),
    )
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one")],
        actions=["group", "arrow"],
        contexts=CONTEXTS,
    )
    assert _check(doc("one"), action, user("alice")).conditional_on == ("b",)


def test_conditional_arrow_into_an_exclusion_under_an_exclusion(install):
    _write(
        install(MISSING),
        (doc("one"), "parent", SubjectRef(doc("two")), "gate", None),
        (doc("two"), "r1", user("alice"), "ca", {"a": True}),
    )
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one"), doc("two")],
        actions=["p", "under"],
        contexts=CONTEXTS,
    )
    alice = user("alice")
    assert _check(doc("one"), "under", alice).conditional_on == ("a", "b")
    assert not _check(doc("one"), "under", alice, {"a": True, "b": True}).allowed
    assert _check(doc("one"), "under", alice, {"a": False, "b": True}).allowed
    assert _check(doc("one"), "under", user("bob")).allowed
    assert _accessible("under", alice) == ["two"]


def test_caveat_on_a_membership_makes_an_uncaveated_relation_conditional(install):
    _write(
        install(MISSING),
        (doc("one"), "r3", _group(), "", None),
        (ObjectRef("test/group", "g"), "member", user("alice"), "gate", None),
    )
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one")],
        actions=["r3", "banned"],
        contexts=CONTEXTS,
    )
    assert _check(doc("one"), "banned", user("alice")).conditional_on == ("a", "b")
    assert _check(doc("one"), "banned", user("bob")).allowed


def test_an_alternative_that_holds_makes_the_other_one_unneeded(install):
    _write(
        install(MISSING),
        (doc("one"), "r1", _group(), "ca", None),
        (ObjectRef("test/group", "g"), "member", user("alice"), "", None),
        (ObjectRef("test/group", "g"), "member", user("alice"), "cb", None),
    )
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one")],
        actions=["r1", "group"],
        contexts=CONTEXTS,
    )
    assert _check(doc("one"), "r1", user("alice")).conditional_on == ("a",)
    assert _check(doc("one"), "r1", user("alice"), {"a": True}).allowed


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
    assert_reads_match(
        subjects=[user("alice"), user("bob")],
        resources=[doc("one"), *anchors],
        actions=["read", "view", "member"],
    )
    assert_subjects_match(
        resources=[doc("one"), *anchors],
        actions=["read", "view", "member"],
        subject_types=["auth/user"],
    )
    assert _check(doc("one"), "read", user("alice")).allowed
    assert set(
        active.accessible(subject=user("alice"), action="member", resource_type="test/roleanchor")
    ) == {"admin", "staff"}
    active.delete_relationships(
        RelationshipFilter(resource_type="test/roleanchor", resource_id="admin")
    )
    assert not _check(doc("one"), "read", user("alice")).allowed


def test_unresolvable_backing_names_the_missing_model(install):
    from rebac.checks import check_field_backed_relations

    # No Django model declares test/unmodelled, so its field backing cannot
    # resolve. Every read that reaches it refuses, and rebac.E009 says why.
    install("""
        definition auth/user {}
        definition test/unmodelled {
            relation owner: auth/user // rebac:field=author
            permission read = owner
        }
    """)
    refusal = r"test/unmodelled#owner does not resolve against the models \(rebac\.E009\)"
    with pytest.raises(SchemaError, match=refusal):
        _check(ObjectRef("test/unmodelled", "one"), "read", user("alice"))
    with pytest.raises(SchemaError, match=refusal):
        _accessible("read", user("alice"), "test/unmodelled")
    assert [
        issue.msg
        for issue in check_field_backed_relations()
        if issue.id == "rebac.E009" and issue.msg.startswith("test/unmodelled#owner")
    ] == [
        "test/unmodelled#owner: field backing: field-backed relation requires a concrete "
        "Django model for resource type test/unmodelled"
    ]
