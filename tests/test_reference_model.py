"""The small-model semantic gate; no database and no SQL.

The default gate retains every named counterexample plus every one/two-leaf
expression and a deterministic sample of larger trees. The full 64,426,320
comparison sweep is opt-in: pytest -m reference_exhaustive -n auto. It is split
by shape, universe, instant, context, and 32 expression shards (at most 1,094
expressions / 15,316 comparisons per case). Scheduling targets, not measured
claims: default reference gate <= 60 seconds; full shard <= 120 seconds on one
worker; full sweep <= 60 minutes on 16 workers. Reconcile measured budgets in
the test phase; never reduce coverage to meet a timing target.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import islice, product
from unittest.mock import patch

import pytest

from rebac.schema.ast import CaveatParam, PermArrow, PermBinOp, Permission, PermRef
from rebac.schema.parser import parse_permission_expression, parse_zed
from rebac.types import CheckResult, ObjectRef, RelationshipTuple, SubjectRef
from tests.reference_model import (
    FALSE,
    TRUE,
    Formula,
    ReferenceModel,
)
from tests.reference_oracle import MemoryWalkerOracle

NOW = datetime(2030, 1, 2, tzinfo=UTC)
DOC = ObjectRef("test/doc", "one")
OTHER = ObjectRef("test/doc", "two")
ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")
CAROL = SubjectRef.of("auth/user", "carol")
GROUP = SubjectRef.of("test/group", "g", "member")
NESTED = SubjectRef.of("test/group", "h", "member")
CONTEXTS = (None, {"a": True}, {"a": True, "b": True}, {"a": False, "b": True})


def schema_for(expression):
    schema = parse_zed("""
        use expiration
        definition auth/user {}
        definition auth/anonymous {}
        caveat gate(a bool, b bool) { a && b }
        caveat ca(a bool) { a }
        caveat cb(b bool) { b }
        definition test/group {
            relation member: auth/user | auth/user:* | test/group#member | auth/user with gate
        }
        definition test/doc {
            relation r1: auth/user | auth/user:* | test/group#member | auth/user with gate | auth/user with ca | test/group#member with gate with expiration
            relation r2: auth/user | auth/user:* | test/group#member | auth/user with gate | auth/user with cb with expiration
            relation parent: test/doc | test/group#member | test/doc with gate with expiration
            relation constant: auth/user // rebac:const=alice
            permission read = nil
        }
    """)
    expr = parse_permission_expression(expression) if isinstance(expression, str) else expression
    schema.definitions[-1] = replace(
        schema.definitions[-1], permissions=(Permission("read", expr),)
    )
    return schema


def row(relation, subject, *, resource=DOC, caveat="", pinned=None, expires=None):
    return RelationshipTuple(resource, relation, subject, caveat, pinned or {}, expires)


def compare(schema, tuples, *, subject=ALICE, resource=DOC, now=NOW, context=None, action="read"):
    reference = ReferenceModel(schema, tuples, now=now)
    oracle = MemoryWalkerOracle(schema, tuples)
    with patch("django.utils.timezone.now", return_value=now):
        expected = oracle.check_access(
            subject=subject, action=action, resource=resource, context=context
        )
    actual = reference.check(subject=subject, action=action, resource=resource, context=context)
    assert actual.result == expected.result, (actual, expected)
    # The walker also reports what failing and unneeded paths would need.
    assert set(actual.conditional_on) <= set(expected.conditional_on), (actual, expected)
    return actual


def test_r1_finite_exclusion_does_not_reexpand_excluded_member():
    schema = schema_for("r1 - r2")
    tuples = [
        row("r1", GROUP),
        row("r2", ALICE),
        row("member", ALICE, resource=GROUP.object),
        row("member", BOB, resource=GROUP.object),
    ]
    assert compare(schema, tuples) == CheckResult.no()
    assert compare(schema, tuples, subject=BOB) == CheckResult.has()
    assert compare(schema, tuples, subject=GROUP) == CheckResult.has()
    tuples.append(row("member", CAROL, resource=GROUP.object))
    assert compare(schema, tuples, subject=CAROL) == CheckResult.has()


@pytest.mark.parametrize(
    "context,expected",
    [
        (None, CheckResult.conditional(("a", "b"))),
        ({"a": True}, CheckResult.conditional(("b",))),
        ({"a": True, "b": True}, CheckResult.no()),
        ({"a": False, "b": True}, CheckResult.has()),
    ],
)
def test_r2_conditional_deny_membership(context, expected):
    schema = schema_for("authenticated - r2")
    tuples = [row("r2", GROUP), row("member", ALICE, resource=GROUP.object, caveat="gate")]
    assert compare(schema, tuples, context=context) == expected
    assert (
        ReferenceModel(schema, tuples, now=NOW).accessible(
            subject=ALICE, action="read", resource_type="test/doc"
        )
        == set()
    )


def test_r3_type_level_constant_minus_concrete_ban():
    schema = schema_for("constant - r2")
    tuples = [row("r2", ALICE)]
    assert compare(schema, tuples) == CheckResult.no()
    assert compare(schema, tuples, resource=OTHER) == CheckResult.has()
    assert compare(schema, tuples, resource=ObjectRef("test/doc", "unseen")) == CheckResult.has()


def test_r5_cofinite_union_regrants_only_alice():
    schema = schema_for("(authenticated - r1) + r2")
    tuples = [
        row("r1", GROUP),
        row("r2", ALICE),
        row("member", ALICE, resource=GROUP.object),
        row("member", BOB, resource=GROUP.object),
    ]
    assert compare(schema, tuples) == CheckResult.has()
    assert compare(schema, tuples, subject=BOB) == CheckResult.no()
    assert compare(schema, tuples, subject=CAROL) == CheckResult.has()


def test_right_exception_regrants_atoms_with_three_state_grouping():
    schema = schema_for("authenticated - (r1 - r2)")
    tuples = [row("r1", ALICE, caveat="ca"), row("r2", ALICE)]
    # a AND NOT(b AND NOT c) is true here, even when b is unknown.
    # (a AND NOT b) OR (a AND b AND c) would incorrectly be conditional.
    assert compare(schema, tuples) == CheckResult.has()


def test_r10_missing_sets_do_not_absorb_a_plus_a_and_b():
    schema = schema_for("r1 + (r1 & r2)")
    tuples = [row("r1", ALICE, caveat="ca"), row("r2", ALICE, caveat="cb")]
    assert compare(schema, tuples) == CheckResult.conditional(("a", "b"))
    assert compare(schema, tuples, context={"a": True}) == CheckResult.has()


def test_missing_sets_include_visited_conditional_path_that_later_fails():
    schema = schema_for("(r1 & r2) + r1")
    tuples = [row("r1", ALICE, caveat="ca"), row("r2", ALICE, caveat="cb", pinned={"b": False})]
    assert compare(schema, tuples) == CheckResult.conditional(("a",))


@pytest.mark.parametrize(
    "actor,authenticated,anonymous,wildcard",
    [
        (ALICE, True, False, True),
        (SubjectRef.of("auth/user", "unseen"), True, False, True),
        (SubjectRef.of("auth/user", "set", "member"), True, False, False),
        (SubjectRef.of("auth/anonymous", "*"), False, True, False),
        (SubjectRef.of("auth/anonymous", "other"), True, False, False),
        (SubjectRef.of("auth/user", ""), False, False, True),
    ],
)
def test_r14_wildcard_and_anonymous_matching(actor, authenticated, anonymous, wildcard):
    for expression, allowed in (("authenticated", authenticated), ("anonymous", anonymous)):
        assert compare(schema_for(expression), [], subject=actor).allowed is allowed
    tuples = [
        row("r1", GROUP),
        row("member", SubjectRef.of("auth/user", "*"), resource=GROUP.object),
    ]
    assert compare(schema_for("r1"), tuples, subject=actor).allowed is wildcard


def test_r14_backed_constant_membership_at_concrete_set():
    schema = schema_for("r1")
    group = schema.get_definition("test/group")
    constant = schema.get_definition("test/doc").relations[-1]
    schema.definitions[schema.definitions.index(group)] = replace(
        group, relations=(replace(constant, name="member"),)
    )
    assert compare(schema, [row("r1", GROUP)]) == CheckResult.has()


@pytest.mark.parametrize(
    "expression,actor,allowed",
    [
        ("r1 & authenticated", SubjectRef.of("auth/user", ""), False),
        ("r1 & authenticated", ALICE, True),
        ("r1 & authenticated", SubjectRef.of("auth/user", "g", "member"), False),
        ("authenticated - r1", SubjectRef.of("auth/user", "g", "member"), True),
        ("authenticated - r1", ALICE, False),
    ],
)
def test_class_set_operations_preserve_empty_id_and_subject_set_rules(expression, actor, allowed):
    tuples = [row("r1", SubjectRef.of("auth/user", "*"))]
    assert compare(schema_for(expression), tuples, subject=actor).allowed is allowed


def test_arrow_follows_subject_object_even_with_subject_set_suffix():
    schema = schema_for("parent->member")
    tuples = [row("parent", GROUP), row("member", ALICE, resource=GROUP.object)]
    assert compare(schema, tuples) == CheckResult.has()


@pytest.mark.parametrize("expression", ["r1", "parent->r1"])
def test_d4_complete_lookup_subjects_intentionally_exceeds_frozen_lookup(expression):
    schema = schema_for(expression)
    tuples = [
        row("r1", GROUP, resource=OTHER if "->" in expression else DOC),
        row("parent", SubjectRef(OTHER)),
        row("member", NESTED, resource=GROUP.object),
        row("member", ALICE, resource=NESTED.object),
    ]
    reference = ReferenceModel(schema, tuples, now=NOW)
    oracle = MemoryWalkerOracle(schema, tuples)
    with patch("django.utils.timezone.now", return_value=NOW):
        assert (
            set(oracle.lookup_subjects(resource=DOC, action="read", subject_type="auth/user"))
            == set()
        )
    assert reference.lookup_subjects(resource=DOC, action="read", subject_type="auth/user") == {
        ALICE
    }
    assert compare(schema, tuples) == CheckResult.has()


@pytest.mark.parametrize("delta,allowed", [(-1, False), (0, True), (1, True)])
def test_ban_expiry_and_intersection_have_half_open_intervals(delta, allowed):
    schema = schema_for("(authenticated - r2) & r1")
    tuples = [row("r1", ALICE), row("r2", ALICE, expires=NOW)]
    assert compare(schema, tuples, now=NOW + timedelta(seconds=delta)).allowed is allowed


def test_override_deadline_propagates_through_permission_and_arrow_and_recaveat():
    before, after = schema_for("r1"), schema_for("r1")
    definition = before.definitions[-1]
    before.definitions[-1] = replace(
        definition,
        permissions=(
            Permission("base", parse_permission_expression("r1 - r2")),
            Permission("read", parse_permission_expression("parent->base")),
        ),
    )
    after.definitions[-1] = replace(
        before.definitions[-1],
        permissions=(
            Permission("base", PermRef("r1")),
            before.definitions[-1].permissions[1],
        ),
    )
    after.caveats = [replace(c, expression="!a") if c.name == "ca" else c for c in after.caveats]
    tuples = [
        row("parent", SubjectRef(OTHER)),
        row("r1", ALICE, resource=OTHER, caveat="ca", pinned={"a": False}),
        row("r2", ALICE, resource=OTHER),
    ]
    for instant, expected in ((NOW - timedelta(seconds=1), False), (NOW, True)):
        model = ReferenceModel(before if instant < NOW else after, tuples, now=instant)
        assert model.check(subject=ALICE, resource=DOC, action="read").allowed is expected
        assert compare(model.schema, tuples, now=instant).allowed is expected


def test_positive_data_cycle_uses_finite_paths_without_depth_limit():
    schema = schema_for("r1 + parent->read")
    tuples = [
        row("parent", SubjectRef(OTHER)),
        row("parent", SubjectRef(DOC), resource=OTHER),
        row("r1", ALICE, resource=OTHER),
    ]
    model = ReferenceModel(schema, tuples, now=NOW)
    assert model.check(subject=ALICE, action="read", resource=DOC) == CheckResult.has()
    assert model.check(subject=BOB, action="read", resource=DOC) == CheckResult.no()


def test_model_level_check_preserves_empty_type_and_existing_row_semantics():
    empty = ObjectRef("test/doc", "")
    assert compare(schema_for("authenticated"), [], resource=empty) == CheckResult.has()
    assert compare(schema_for("r1"), [row("r1", ALICE)], resource=empty) == CheckResult.has()
    assert compare(schema_for("r1"), [], resource=empty) == CheckResult.no()


def test_fully_pinned_params_do_not_erase_missing_declared_runtime_condition():
    schema = schema_for("authenticated - r1")
    schema.caveats = [
        replace(
            c,
            params=(*c.params, CaveatParam("runtime_flag", "bool")),
            expression="a && runtime_flag",
        )
        if c.name == "ca"
        else c
        for c in schema.caveats
    ]
    tuples = [row("r1", ALICE, caveat="ca", pinned={"a": True})]
    result = compare(schema, tuples)
    assert result == CheckResult.conditional(("runtime_flag",))


def test_reference_schema_rejects_undeclared_runtime_identifier():
    from rebac.schema.parser import validate_schema

    schema = schema_for("r1")
    schema.caveats = [
        replace(c, expression="a && runtime_flag") if c.name == "ca" else c for c in schema.caveats
    ]
    assert any("undeclared identifier 'runtime_flag'" in error for error in validate_schema(schema))


def test_alternative_membership_paths_keep_all_missing_sets_and_last_expiry():
    schema = schema_for("r1")
    tuples = [
        row("r1", GROUP, caveat="gate", pinned={"a": True}),
        row("r1", GROUP, caveat="gate", pinned={"b": True}, expires=NOW),
        row("member", ALICE, resource=GROUP.object),
    ]
    assert compare(schema, tuples, now=NOW - timedelta(seconds=1)) == CheckResult.conditional(
        ("a", "b")
    )
    assert compare(schema, tuples, now=NOW) == CheckResult.conditional(("b",))


def test_exact_intersection_userset_actor_does_not_leak_to_its_members():
    schema = schema_for("(r1 - r2) & r1")
    tuples = [row("r1", GROUP), row("r2", ALICE), row("member", ALICE, resource=GROUP.object)]
    assert compare(schema, tuples) == CheckResult.no()
    assert compare(schema, tuples, subject=GROUP) == CheckResult.has()


def _trees(leaves):
    if leaves == 1:
        yield None
    else:
        for size in range(1, leaves):
            yield from product(_trees(size), _trees(leaves - size))


LEAVES = (
    PermRef("r1"),
    PermRef("r2"),
    PermArrow("parent", "r1"),
    PermRef("authenticated"),
    PermRef("anonymous"),
    PermRef("constant"),
)


def _expressions(shape):
    if shape is None:
        yield from LEAVES
    else:
        for left, right, op in product(_expressions(shape[0]), _expressions(shape[1]), "+&-"):
            yield PermBinOp(op, left, right)


SHAPES = [
    pytest.param(shape, id=f"leaves-{size}-shape-{number}")
    for size in range(1, 5)
    for number, shape in enumerate(_trees(size))
]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("users", [1, 2, 3])
@pytest.mark.parametrize("instant", [-1, 0, 1])
@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("shard", range(32))
@pytest.mark.reference_exhaustive
@pytest.mark.reference_shard
def test_exhaustive_expression_small_models(shape, users, instant, context, shard):
    _compare_expressions(islice(_expressions(shape), shard, None, 32), users, instant, context)


def _compare_expressions(expressions, users, instant, context):
    """All trees/labels, two resources, up to three users and two nested groups."""
    actors = (ALICE, BOB, CAROL)[:users]
    tuples = [
        row("r1", ALICE, caveat="gate", expires=NOW),
        row("r2", actors[-1]),
        row("parent", SubjectRef(OTHER)),
        row("r1", actors[-1], resource=OTHER),
    ]
    if users >= 2:
        tuples += [row("r1", GROUP), row("member", BOB, resource=GROUP.object)]
        actors += (GROUP,)
    if users == 3:
        tuples += [
            row("member", NESTED, resource=GROUP.object),
            row("member", SubjectRef.of("auth/user", "*"), resource=NESTED.object),
            row("r2", CAROL, resource=OTHER, caveat="gate"),
        ]
        actors += (NESTED,)
    actors += (SubjectRef.of("auth/anonymous", "*"), SubjectRef.of("auth/user", ""))
    schema = schema_for("nil")
    now = NOW + timedelta(seconds=instant)
    oracle = MemoryWalkerOracle(schema, tuples)
    reference = ReferenceModel(schema, tuples, now=now)
    definition = schema.definitions[-1]
    with patch("django.utils.timezone.now", return_value=now):
        for expression in expressions:
            schema.definitions[-1] = replace(
                definition, permissions=(Permission("read", expression),)
            )
            for actor, resource in product(actors, (DOC, OTHER)):
                expected = oracle.check_access(
                    subject=actor, action="read", resource=resource, context=context
                )
                actual = reference.check(
                    subject=actor, action="read", resource=resource, context=context
                )
                assert (actual.result, actual.conditional_on) == (
                    expected.result,
                    expected.conditional_on,
                ), (expression, tuples, actor, resource, now, context)


@pytest.mark.parametrize("op", "+&-")
@pytest.mark.parametrize("users", [1, 2, 3])
@pytest.mark.parametrize("shard", range(16))
@pytest.mark.reference_exhaustive
@pytest.mark.reference_shard
def test_exhaustive_direct_tuple_presence(op, users, shard):
    _compare_presence(op, users, shard=shard, shards=16)


def _compare_presence(op, users, *, shard=0, shards=1):
    actors = (ALICE, BOB, CAROL)[:users]
    schema = schema_for(f"r1 {op} r2")
    possible = [
        row(relation, actor, resource=resource)
        for resource, relation, actor in product((DOC, OTHER), ("r1", "r2"), actors)
    ]
    for mask in range(shard, 1 << len(possible), shards):
        tuples = [item for bit, item in enumerate(possible) if mask & (1 << bit)]
        for actor, resource in product(actors, (DOC, OTHER)):
            compare(schema, tuples, subject=actor, resource=resource)


@pytest.mark.parametrize("instant", [-1, 0, 1])
@pytest.mark.parametrize("context", CONTEXTS)
def test_default_expression_gate(instant, context):
    # The largest graph remains in the default run. All small expressions and
    # deterministic samples of every larger tree keep this affordable.
    for size in range(1, 5):
        for shape in _trees(size):
            expressions = _expressions(shape)
            if size > 2:
                expressions = islice(expressions, 0, None, 257)
                expressions = islice(expressions, 4)
            _compare_expressions(expressions, 3, instant, context)


@pytest.mark.parametrize("op", "+&-")
def test_default_direct_tuple_presence(op):
    _compare_presence(op, 1)


def test_formula_short_circuit_preserves_missing_and_unknown_caveats_fail_closed():
    schema = schema_for("nil")
    for formula, expected, missing in (
        (Formula("or", (TRUE, Formula("caveat", name="ca"))), True, set()),
        (Formula("and", (FALSE, Formula("caveat", name="ca"))), False, set()),
        (Formula("caveat", name="missing"), False, set()),
    ):
        assert formula.evaluate(schema, None) == (expected, missing)


# Cases from the semantic review of the index design. Here the reference and
# the frozen walker are compared in memory; the derivation, read and
# maintenance suites compare the index with both.

CONSTANT_ARROW = """
    definition auth/user {}
    definition auth/anonymous {}
    definition test/org {
        relation member: auth/user
        relation banned: auth/user
        permission manage = authenticated - banned
        permission staff = member - banned
    }
    definition test/doc {
        relation org: test/org // rebac:const=default
        permission read = org->manage
        permission edit = org->staff
    }
"""
ORG = ObjectRef("test/org", "default")
UNSEEN = ObjectRef("test/doc", "unseen")


def schema_with(**permissions):
    schema = schema_for("nil")
    schema.definitions[-1] = replace(
        schema.definitions[-1],
        permissions=tuple(
            Permission(name, parse_permission_expression(text))
            for name, text in permissions.items()
        ),
    )
    return schema


@pytest.mark.parametrize("resource", [DOC, UNSEEN])
def test_site_behind_a_constant_arrow_is_evaluated_at_the_target(resource):
    schema = parse_zed(CONSTANT_ARROW)
    tuples = [
        row("banned", ALICE, resource=ORG),
        row("member", ALICE, resource=ORG),
        row("member", BOB, resource=ORG),
    ]
    expected = {
        "read": {ALICE: False, BOB: True, CAROL: True},
        "edit": {ALICE: False, BOB: True, CAROL: False},
    }
    for action, subjects in expected.items():
        for subject, allowed in subjects.items():
            result = compare(schema, tuples, subject=subject, resource=resource, action=action)
            assert result.allowed is allowed, (action, subject)


def test_type_level_site_behind_an_arrow_is_evaluated_at_the_target():
    schema = schema_with(p="authenticated - r2", read="parent->p")
    tuples = [row("parent", SubjectRef(OTHER)), row("r2", ALICE, resource=OTHER)]
    assert compare(schema, tuples, subject=ALICE) == CheckResult.no()
    assert compare(schema, tuples, subject=BOB) == CheckResult.has()


@pytest.mark.parametrize("tuples", [[], [row("r1", ALICE)]])
def test_model_level_check_of_a_type_level_site_ignores_concrete_bans(tuples):
    schema = schema_for("authenticated - r1")
    assert compare(schema, tuples, resource=ObjectRef("test/doc", "")) == CheckResult.has()


def test_subject_enumeration_lists_named_subjects_and_nobody_by_class():
    tuples = [
        row("r1", GROUP),
        row("member", ALICE, resource=GROUP.object),
        row("member", BOB, resource=GROUP.object),
        row("member", NESTED, resource=GROUP.object),
        row("member", CAROL, resource=NESTED.object),
        row("r2", ALICE),
        row("r1", SubjectRef.of("auth/user", "dave"), resource=OTHER),
    ]

    def listed(expression, resource=DOC):
        reference = ReferenceModel(schema_for(expression), tuples, now=NOW)
        return reference.lookup_subjects(resource=resource, action="read", subject_type="auth/user")

    assert listed("r1 - r2") == {BOB, CAROL}
    # An intersection is within both operands, whichever comes first.
    assert listed("r1 & (authenticated - r2)") == {BOB, CAROL}
    assert listed("(authenticated - r2) & r1") == {BOB, CAROL}
    # Every authenticated user passes this check, yet no tuple names one.
    assert listed("authenticated - r2") == set()
    # A subject named on another resource is not named here.
    assert listed("authenticated + r1") == {ALICE, BOB, CAROL}
    assert listed("authenticated + r1", OTHER) == {SubjectRef.of("auth/user", "dave")}


def _walked(schema, tuples, *, subject=ALICE, context=None):
    with patch("django.utils.timezone.now", return_value=NOW):
        return MemoryWalkerOracle(schema, tuples).check_access(
            subject=subject, action="read", resource=DOC, context=context
        )


@pytest.mark.parametrize(
    "expression,walker",
    [("(r1 + r2) - parent->r2", ("a", "b")), ("(r2 + r1) - parent->r2", ("b",))],
)
def test_missing_parameters_do_not_depend_on_the_order_of_arms(expression, walker):
    schema = schema_for(expression)
    tuples = [
        row("r1", ALICE, caveat="ca"),
        row("r2", ALICE),
        row("parent", SubjectRef(OTHER)),
        row("r2", ALICE, resource=OTHER, caveat="cb"),
    ]
    # The left side holds through r2, so only the exclusion is open.
    assert compare(schema, tuples) == CheckResult.conditional(("b",))
    assert _walked(schema, tuples).conditional_on == walker


@pytest.mark.parametrize(
    "expression,failing",
    [
        (
            "r1 + r2",
            [
                row("r1", GROUP, caveat="gate", pinned={"b": True}),
                row("member", BOB, resource=GROUP.object),
            ],
        ),
        ("parent->r1 + r2", [row("parent", SubjectRef(OTHER), caveat="gate", pinned={"b": True})]),
    ],
)
def test_a_path_that_cannot_hold_needs_no_parameter(expression, failing):
    schema = schema_for(expression)
    tuples = [*failing, row("r2", ALICE, caveat="cb")]
    assert compare(schema, tuples) == CheckResult.conditional(("b",))
    assert _walked(schema, tuples).conditional_on == ("a", "b")


def test_conditional_arrow_into_a_site_under_an_exclusion():
    schema = schema_with(p="r1 - r2", read="authenticated - parent->p")
    tuples = [row("parent", SubjectRef(OTHER), caveat="gate"), row("r1", ALICE, resource=OTHER)]
    assert compare(schema, tuples) == CheckResult.conditional(("a", "b"))
    assert compare(schema, tuples, context={"a": True, "b": True}) == CheckResult.no()
    assert compare(schema, tuples, context={"a": False, "b": True}) == CheckResult.has()
    assert compare(schema, tuples, subject=BOB) == CheckResult.has()
    reference = ReferenceModel(schema, tuples, now=NOW)
    assert reference.accessible(subject=ALICE, action="read", resource_type="test/doc") == {"two"}


CAVEATED_MEMBERSHIP = """
    caveat gate(a bool, b bool) { a && b }
    caveat ca(a bool) { a }
    caveat cb(b bool) { b }
    definition auth/user {}
    definition auth/anonymous {}
    definition test/group { relation member: auth/user | auth/user with gate | auth/user with cb }
    definition test/doc {
        relation r1: test/group#member with ca
        relation r2: test/group#member
        permission banned = authenticated - r2
        permission read = r1
    }
"""


def test_caveat_on_a_membership_makes_an_uncaveated_relation_conditional():
    schema = parse_zed(CAVEATED_MEMBERSHIP)
    tuples = [row("r2", GROUP), row("member", ALICE, resource=GROUP.object, caveat="gate")]
    assert compare(schema, tuples, action="banned") == CheckResult.conditional(("a", "b"))
    assert compare(schema, tuples, action="banned", subject=BOB) == CheckResult.has()


def test_an_alternative_that_holds_makes_the_other_one_unneeded():
    schema = parse_zed(CAVEATED_MEMBERSHIP)
    tuples = [
        row("r1", GROUP, caveat="ca"),
        row("member", ALICE, resource=GROUP.object),
        row("member", ALICE, resource=GROUP.object, caveat="cb"),
    ]
    # Alice is a member whatever b is, so only the hop's caveat is open.
    assert compare(schema, tuples) == CheckResult.conditional(("a",))
    assert compare(schema, tuples, context={"a": True}) == CheckResult.has()
