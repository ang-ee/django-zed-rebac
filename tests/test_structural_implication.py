"""The implication structural delivery relies on (proposal 0022): holding one action grants another.

``structural.implies(policy, type, held, granted)`` proves, from the policy's text, that whoever
holds ``held`` on a row also holds ``granted`` on it. It may fail to prove a true implication; it
must never prove a false one. One test per rule, one per refusal, and a randomized check of every
implication the prover claims against what the compiled reads return.
"""

from __future__ import annotations

import itertools
import random
from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from rebac import LocalBackend, ObjectRef, RelationshipTuple, SubjectRef, structural
from rebac.compile import read
from rebac.models import SchemaDefinition, SchemaOverride, SchemaPermission, SchemaRelation
from rebac.testing import install_schema
from tests.mechanism_policies import seeds
from tests.testapp.models import ScopeMessage, ScopeThread

pytestmark = pytest.mark.django_db

POLICY = """
use expiration
caveat enabled(ok bool) {{ ok }}
definition auth/user {{}}
definition auth/group {{ relation member: auth/user }}
definition doc/folder {{
    relation viewer: auth/user
    relation editor: auth/user
    relation x: auth/user
    relation parent: doc/folder
    permission read = viewer + editor
    permission inbox = viewer
    permission tree_read = (parent->tree_read + viewer) + editor
    permission tree_inbox = parent->tree_inbox + viewer
    permission below_read = parent->tree_read
    permission tree_or_below = (parent->tree_read + viewer) + editor
    permission x_but_not_below = x - ((parent->tree_read + viewer) + editor)
    permission x_but_not_tree = x - tree_read
    permission x_but_not_lifted_below = x - parent->tree_or_below
    permission x_but_not_lifted_tree = x - parent->tree_read
}}
definition doc/shelf {{
    relation viewer: auth/user
    relation keeper: auth/user
    permission read = keeper
    permission inbox = viewer
}}
definition doc/doc {{
    relation viewer: auth/user
    relation editor: auth/user
    relation owner: auth/user
    relation banned: auth/user
    relation muted: auth/user
    relation flagged: auth/user with enabled
    relation public: auth/user:*
    relation folder: doc/folder
    relation other_folder: doc/folder
    relation place: doc/folder | doc/shelf
    {permissions}
}}
"""


def implied(permissions: str, held: str = "a", granted: str = "t", type_: str = "doc/doc") -> bool:
    schema = POLICY.format(permissions=permissions)
    return structural.implies(read._policy(install_schema(schema)), type_, held, granted)


def held_and_granted(a: str, t: str, *extra: str) -> str:
    """The permissions ``a`` and ``t`` of ``doc/doc``, beside any ``extra`` ones."""
    return "\n    ".join([*extra, f"permission a = {a}", f"permission t = {t}"])


RULES = [
    pytest.param(held_and_granted("viewer + editor", "viewer + editor"), id="equal text"),
    pytest.param(held_and_granted("nil", "viewer"), id="nil grants nothing"),
    pytest.param(held_and_granted("viewer", "viewer + editor"), id="an operand of the union"),
    pytest.param(
        held_and_granted("editor + viewer", "(viewer + owner) + editor"), id="each operand"
    ),
    pytest.param(held_and_granted("viewer & editor", "viewer + owner"), id="a conjunct dropped"),
    pytest.param(
        held_and_granted("viewer & editor", "editor & viewer"), id="each conjunct implied"
    ),
    pytest.param(
        held_and_granted("(viewer + editor) - banned", "((viewer + editor) + owner) - banned"),
        id="the same subtrahend",
    ),
    pytest.param(
        held_and_granted("viewer - (banned + muted)", "viewer - banned"),
        id="a subtrahend that withholds less",
    ),
    pytest.param(held_and_granted("viewer - banned", "viewer + owner"), id="the minuend implies"),
    pytest.param(
        held_and_granted("base", "base + editor", "permission base = viewer"),
        id="named permissions opened",
    ),
    pytest.param(held_and_granted("folder->inbox", "folder->read + owner"), id="an arrow lifted"),
    pytest.param(held_and_granted("flagged", "flagged + owner"), id="a caveated grant"),
    pytest.param(held_and_granted("public", "owner + public"), id="a wildcard grant"),
    # Inside an expression a built-in name is the actor's state, whatever is declared under it.
    pytest.param(
        held_and_granted(
            "authenticated", "authenticated + owner", "permission authenticated = owner"
        ),
        id="a built-in name equals itself",
    ),
]

REFUSALS = [
    # A union on the left implies only what each of its operands does.
    pytest.param(held_and_granted("viewer + banned", "viewer + editor"), id="an extra operand"),
    # An intersection on the right needs every conjunct implied.
    pytest.param(held_and_granted("viewer", "viewer & editor"), id="an added conjunct"),
    # A subtrahend on the right must withhold no more than the left's.
    pytest.param(
        held_and_granted("viewer - banned", "viewer - muted"), id="a differing subtrahend"
    ),
    pytest.param(
        held_and_granted("viewer - banned", "viewer - (banned + muted)"),
        id="a subtrahend that withholds more",
    ),
    # An exclusion added on the right needs the two disjoint.
    pytest.param(held_and_granted("viewer", "viewer - banned"), id="an added exclusion"),
    # Two relations are two sets, whatever their subjects.
    pytest.param(held_and_granted("viewer", "editor"), id="another relation"),
    # A caveated grant implies only itself, under its own condition.
    pytest.param(held_and_granted("flagged", "viewer"), id="a caveated arm on one side"),
    # ``place`` reaches shelves too, where ``inbox`` does not grant ``read``.
    pytest.param(held_and_granted("place->inbox", "place->read"), id="a multi-type relation"),
    pytest.param(held_and_granted("folder->read", "folder->inbox"), id="the lifted converse"),
    # Two relations to one type are two sets of edges.
    pytest.param(
        held_and_granted("folder->read", "other_folder->read"), id="two relations of one type"
    ),
    # Inside an expression ``authenticated`` is the built-in, not the permission of that name.
    pytest.param(
        held_and_granted("authenticated", "viewer + owner", "permission authenticated = viewer"),
        id="a permission named like a built-in",
    ),
    pytest.param(held_and_granted("viewer", "nil"), id="nil on the right"),
    # What a subtrahend withholds is no grant.
    pytest.param(held_and_granted("editor - viewer", "viewer"), id="a subtrahend is no grant"),
    # A goal met again is not assumed while it is proven: p and q are viewer + editor.
    pytest.param(
        held_and_granted(
            "p & q", "editor + owner", "permission p = q + viewer", "permission q = p + editor"
        ),
        id="an alias cycle on the held side",
    ),
]


@pytest.mark.parametrize("permissions", RULES)
def test_each_rule_proves(permissions):
    assert implied(permissions)


@pytest.mark.parametrize("permissions", REFUSALS)
def test_each_refusal(permissions):
    assert not implied(permissions)


def test_a_recursion_is_refused_and_an_action_still_implies_itself():
    """``tree_inbox`` does grant ``tree_read`` (by induction on the parent chain), but the compiled
    reading of a recursion stops at the depth limit, which the rules do not model."""
    assert not implied("", "tree_inbox", "tree_read", "doc/folder")
    assert implied("", "tree_read", "tree_read", "doc/folder")
    assert implied("", "inbox", "read", "doc/folder")


def test_a_recursion_on_the_granted_side_is_not_opened():
    """``parent->tree_read`` is an operand of ``tree_read``'s union, but ``tree_read`` reads it with
    one hop less of the depth limit than ``below_read`` does: a chain one hop longer than the
    limit is read by ``below_read`` and not by ``tree_read``."""
    assert not implied("", "below_read", "tree_read", "doc/folder")
    # The other way round the recursion is on the held side, read with as much depth or less.
    assert implied("", "tree_read", "tree_or_below", "doc/folder")


@pytest.mark.pg_delta
@pytest.mark.parametrize("held", ["below_read", "tree_read"])
def test_what_the_recursion_refusal_protects_against(settings, held):
    settings.REBAC_DEPTH_LIMIT = 2
    local = install_schema(POLICY.format(permissions=""))
    alice = SubjectRef.of("auth/user", "alice")
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("doc/folder", "f0"), "viewer", alice),
            *(
                RelationshipTuple(
                    ObjectRef("doc/folder", f"f{n}"),
                    "parent",
                    SubjectRef.of("doc/folder", f"f{n - 1}"),
                )
                for n in (1, 2, 3)
            ),
        ]
    )

    def granted(action: str) -> set[str]:
        return set(local.accessible(subject=alice, action=action, resource_type="doc/folder"))

    assert granted("tree_read") == {"f0", "f1", "f2"}
    if held == "below_read":
        # Three hops from f3 to the viewer's folder: past the limit for tree_read alone.
        assert granted(held) == {"f1", "f2", "f3"}
        assert not structural.implies(read._policy(local), "doc/folder", held, "tree_read")
    else:
        assert granted(held) <= granted("tree_or_below")
        assert structural.implies(read._policy(local), "doc/folder", held, "tree_or_below")


def test_a_recursion_in_a_subtrahend_is_opened_on_neither_side():
    """A subtrahend is read at the opposite bound, where a recursion cut at the limit counts as
    holding at its frontier: less depth withholds more. ``tree_read`` opened on the held side
    of the subtrahends' goal would compare it with ``parent->tree_read``, read deeper."""
    assert not implied("", "x_but_not_below", "x_but_not_tree", "doc/folder")
    assert not implied("", "x_but_not_lifted_below", "x_but_not_lifted_tree", "doc/folder")
    assert implied("", "x_but_not_tree", "x_but_not_tree", "doc/folder")


@pytest.mark.pg_delta
def test_what_the_subtrahend_refusal_protects_against(settings):
    settings.REBAC_DEPTH_LIMIT = 2
    local = install_schema(POLICY.format(permissions=""))
    alice = SubjectRef.of("auth/user", "alice")
    # Another chain of folders, one hop longer than the limit, alice the viewer of its root:
    # the recursion's closure does not converge within the limit.
    tuples = [
        RelationshipTuple(ObjectRef("doc/folder", "f0"), "x", alice),
        RelationshipTuple(ObjectRef("doc/folder", "g0"), "viewer", alice),
        *(
            RelationshipTuple(
                ObjectRef("doc/folder", f"g{n}"), "parent", SubjectRef.of("doc/folder", f"g{n - 1}")
            )
            for n in (1, 2, 3)
        ),
    ]
    local.write_relationships(tuples)

    def granted(action: str) -> set[str]:
        return set(local.accessible(subject=alice, action=action, resource_type="doc/folder"))

    assert "f0" in granted("x_but_not_below")
    assert "f0" not in granted("x_but_not_tree")


@pytest.mark.pg_delta
def test_a_goal_is_kept_with_whether_it_sits_in_a_subtrahend(settings):
    """``parent->tree + viewer => tree`` is refused in a subtrahend and not out of one: one goal,
    two answers. Kept without the difference, the first would answer the second."""
    from tests.testapp.models import Folder

    settings.REBAC_DEPTH_LIMIT = 2
    local = install_schema(
        """
        definition auth/user {}
        definition blog/folder {
            relation parent: blog/folder // rebac:field=parent
            relation viewer: auth/user
            relation x: auth/user
            permission tree = parent->tree + viewer
            permission a = tree + (x - (parent->tree + viewer))
            permission t = (x - tree) + (parent->tree + viewer)
        }
        """
    )
    Folder._base_manager.bulk_create(
        [Folder(id=n, name=f"f{n}", parent_id=n - 1 if n > 1 else None) for n in (1, 2, 3, 4)]
    )
    alice = SubjectRef.of("auth/user", "alice")
    local.write_relationships([RelationshipTuple(ObjectRef("blog/folder", "4"), "x", alice)])

    def granted(action: str) -> set[str]:
        return set(local.accessible(subject=alice, action=action, resource_type="blog/folder"))

    assert granted("a") == {"4"} and granted("t") == set()
    assert not structural.implies(read._policy(local), "blog/folder", "a", "t")


def test_each_implication_is_kept_under_both_its_names():
    schema = POLICY.format(
        permissions=held_and_granted(
            "viewer", "viewer + owner", "permission b = editor", "permission u = owner"
        )
    )
    policy = read._policy(install_schema(schema))
    assert structural.implies(policy, "doc/doc", "a", "t")
    assert not structural.implies(policy, "doc/doc", "b", "t")
    assert not structural.implies(policy, "doc/doc", "a", "u")


def test_a_goal_is_kept_with_its_type():
    """``p => q`` holds on messages and not on threads: one goal of two types."""
    schema = """
    definition auth/user {}
    definition scope/thread {
        relation reader: auth/user
        relation editor: auth/user
        permission p = reader
        permission q = editor
        permission z = p
        permission zz = q
    }
    definition scope/message {
        relation thread: scope/thread
        relation viewer: auth/user
        relation owner: auth/user
        permission p = viewer
        permission q = viewer + owner
        permission a = p & thread->z
        permission t = q & thread->zz
    }
    """
    policy = read._policy(install_schema(schema))
    assert structural.implies(policy, "scope/message", "p", "q")
    assert not structural.implies(policy, "scope/thread", "p", "q")
    assert not structural.implies(policy, "scope/message", "a", "t")


def test_the_proof_is_kept_per_policy():
    schema = POLICY.format(permissions="permission a = viewer\n permission t = viewer + editor")
    policy = read._policy(install_schema(schema))
    assert structural.implies(policy, "doc/doc", "a", "t")
    assert ("implies", policy.key, "doc/doc", "a", "t") in structural._PROOFS
    read.reset()
    assert ("implies", policy.key, "doc/doc", "a", "t") not in structural._PROOFS


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("seed", seeds(20, fast=2))
def test_nothing_is_proven_about_a_type_that_reads_a_name_two_ways(seed, settings, monkeypatch):
    """The randomized policies with the thread type reading a name two ways, a relation named
    ``authenticated`` (odd seeds) or a relation and a permission both named ``al1`` (even), as a
    stored policy from before that rule was held at runtime could: nothing is proven about the
    thread but ``held == granted``, and every claim about the message, whose arrows lift to the
    thread, holds on the data. Before, a relation ``authenticated`` compared equal to the built-in
    of that name inside an expression."""
    from rebac.backends import local as local_module

    monkeypatch.setattr(local_module, "_name_conflicts", lambda schema: [])
    settings.REBAC_DEPTH_LIMIT = 2
    schema, tuples = implication_policy(seed, conflicting=True)
    local = install_schema(schema)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1), ScopeThread(id=2)])
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=n, thread_id=1 if n < 3 else 2) for n in (1, 2, 3)]
    )
    if tuples:
        local.write_relationships(tuples)
    policy = read._policy(local)
    assert policy.name_conflicts
    thread = ("p0", "p1", "p2", "p3", "al1", "al2", "authenticated")
    for held, wanted in itertools.permutations(thread, 2):
        assert not structural.implies(policy, "scope/thread", held, wanted), (seed, held, wanted)
    for held, wanted in itertools.permutations(("p0", "p1", "p2"), 2):
        if structural.implies(policy, "scope/message", held, wanted):
            for actor in ACTORS:
                assert set(
                    local.accessible(subject=actor, action=held, resource_type="scope/message")
                ) <= set(
                    local.accessible(subject=actor, action=wanted, resource_type="scope/message")
                ), (seed, held, wanted, actor)


# The reviewer's policy: a relation ``authenticated`` the thread's ``read`` names, which inside
# the expression is the actor's state.
BUILT_IN_RELATION = """
definition auth/user {}
definition scope/thread {
    relation authenticated: auth/user
    permission read = authenticated
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    permission read = thread->authenticated
    permission browse = thread->read
}
"""


def test_a_relation_named_as_a_built_in_is_not_the_built_in(monkeypatch):
    """``read = authenticated`` is every authenticated actor; the relation ``authenticated`` is
    the actors stored in it. The prover equated the two: ``read => authenticated`` on the thread
    and ``browse => read`` on the message, which no stored tuple supports. Installed past the
    runtime check, as a stored policy from before the rule could be."""
    from rebac.backends import local as local_module

    monkeypatch.setattr(local_module, "_name_conflicts", lambda schema: [])
    policy = read._policy(install_schema(BUILT_IN_RELATION))
    assert not structural.implies(policy, "scope/thread", "read", "authenticated")
    assert not structural.implies(policy, "scope/message", "browse", "read")
    assert structural.implies(policy, "scope/thread", "read", "read")
    reason = structural.explain(
        policy,
        child_model=ScopeMessage,
        fk_name="thread",
        parent_model=ScopeThread,
        child_action="read",
        parent_action="read",
    )
    assert reason is not None and "reserved built-in actors" in reason


# ---------- Overrides: the proof reads the policy the backend evaluates ----------


def stored_policy(permissions: dict[str, str]) -> SchemaDefinition:
    """``doc/doc`` stored as schema rows, so that an override can name its permissions."""
    SchemaDefinition.objects.create(resource_type="auth/user")
    definition = SchemaDefinition.objects.create(resource_type="doc/doc")
    for name in ("viewer", "editor", "owner"):
        SchemaRelation.objects.create(
            definition=definition, name=name, allowed_subjects=[{"type": "auth/user"}]
        )
    for name, expression in permissions.items():
        SchemaPermission.objects.create(definition=definition, name=name, expression=expression)
    return definition


def override(definition, permission: str, kind: str, expression: str, expires_at=None) -> None:
    SchemaOverride.objects.create(
        kind=kind,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=SchemaPermission.objects.get(definition=definition, name=permission).pk,
        expression=expression,
        reason="test",
        expires_at=expires_at,
    )


def stored_implies(held: str = "a", granted: str = "t") -> bool:
    return structural.implies(read._policy(LocalBackend()), "doc/doc", held, granted)


def test_an_override_on_the_left_only_is_refused():
    definition = stored_policy({"a": "viewer", "t": "viewer + owner"})
    assert stored_implies()
    # ``a`` now admits editors, whom ``t`` does not.
    override(definition, "a", SchemaOverride.KIND_EXTEND, "editor")
    assert not stored_implies()
    assert stored_implies("t", "t")


def test_an_override_that_can_expire_on_the_right_is_refused():
    definition = stored_policy({"a": "editor", "t": "viewer + owner"})
    assert not stored_implies()
    # ``t`` admits editors until tomorrow: true today, false after the deadline.
    override(
        definition, "t", SchemaOverride.KIND_EXTEND, "editor", timezone.now() + timedelta(days=1)
    )
    assert not stored_implies()


def stored_threads() -> None:
    """``scope/thread`` and ``scope/message`` stored as schema rows, with a caveat, so that an
    override of each kind can name a permission or the caveat."""
    from rebac.models import SchemaCaveat

    SchemaDefinition.objects.create(resource_type="auth/user")
    thread = SchemaDefinition.objects.create(resource_type="scope/thread")
    message = SchemaDefinition.objects.create(resource_type="scope/message")
    SchemaCaveat.objects.create(
        name="enabled", params=[{"name": "ok", "type": "bool"}], expression="ok"
    )
    SchemaRelation.objects.create(
        definition=thread, name="reader", allowed_subjects=[{"type": "auth/user"}]
    )
    SchemaRelation.objects.create(
        definition=thread,
        name="cav",
        allowed_subjects=[{"type": "auth/user", "caveat": "enabled"}],
    )
    SchemaPermission.objects.create(definition=thread, name="a", expression="cav")
    SchemaPermission.objects.create(definition=thread, name="t", expression="cav + reader")
    SchemaRelation.objects.create(
        definition=message,
        name="thread",
        allowed_subjects=[{"type": "scope/thread"}],
        backing={"kind": "fk", "path": "thread"},
    )
    SchemaPermission.objects.create(definition=message, name="read", expression="thread->t")


def stored_override(kind: str, expression: str, expires_at=None) -> None:
    from rebac.models import SchemaCaveat

    if kind == SchemaOverride.KIND_RECAVEAT:
        target, target_pk = SchemaCaveat, SchemaCaveat.objects.get(name="enabled").pk
    else:
        target = SchemaPermission
        target_pk = SchemaPermission.objects.get(
            definition__resource_type="scope/thread", name="a"
        ).pk
    SchemaOverride.objects.create(
        kind=kind,
        target_ct=ContentType.objects.get_for_model(target),
        target_pk=target_pk,
        expression=expression,
        reason="test",
        expires_at=expires_at,
    )


def delivered_edge() -> str | None:
    return structural.explain(
        read._policy(LocalBackend()),
        child_model=ScopeMessage,
        fk_name="thread",
        parent_model=ScopeThread,
        child_action="read",
        parent_action="a",
    )


@pytest.mark.parametrize(
    ("kind", "expression"),
    [
        (SchemaOverride.KIND_EXTEND, "reader"),
        (SchemaOverride.KIND_LOOSEN, "reader"),
        (SchemaOverride.KIND_DISABLE, "reader"),
        (SchemaOverride.KIND_TIGHTEN, "reader"),
        (SchemaOverride.KIND_RECAVEAT, "true"),
    ],
)
@pytest.mark.parametrize("deadline", [None, timedelta(minutes=5), -timedelta(minutes=5)])
def test_no_proof_and_no_delivery_while_any_override_is_stored(kind, expression, deadline):
    stored_threads()
    assert delivered_edge() is None
    assert structural.implies(read._policy(LocalBackend()), "scope/thread", "a", "t")
    stored_override(kind, expression, timezone.now() + deadline if deadline is not None else None)
    assert delivered_edge() == "the policy carries an override"
    assert not structural.implies(read._policy(LocalBackend()), "scope/thread", "a", "t")
    assert structural.implies(read._policy(LocalBackend()), "scope/thread", "a", "a")


# ---------- Randomized: every implication the prover claims holds on the data ----------

ACTORS = (
    SubjectRef.of("auth/user", "member"),
    SubjectRef.of("auth/user", "admin"),
    SubjectRef.of("auth/user", "other"),
)
GROUP = SubjectRef.of("auth/group", "g", "member")


def implication_policy(seed: int, conflicting: bool = False) -> tuple[str, list[RelationshipTuple]]:
    """``(schema, tuples)``: thread permissions derived from one another by shapes the prover
    accepts and shapes it must refuse, over a permission named ``authenticated`` and an alias
    cycle; message permissions of the same names, through two relations to threads; folder
    permissions beside a recursion over a chain longer than the depth limit, and exclusions
    of a recursion; and data."""
    rng = random.Random(seed)
    directional = ["union", "both", "minus", "swap", "nested"]

    def pick(atoms: list[str], denials: list[str], depth: int = 2) -> str:
        if depth == 0 or rng.random() < 0.4:
            return rng.choice(atoms)
        op = rng.choice(["+", "+", "&", "-"])
        right = denials if op == "-" else atoms
        return f"({pick(atoms, denials, depth - 1)} {op} {pick(right, denials, depth - 1)})"

    def derived(text: str, atoms: list[str], denials: list[str], shapes: list[str]) -> str:
        """``text`` grown by a shape: implied by it (union, swap), implying it (both, minus,
        nested), or neither (kept)."""
        shape = rng.choice(shapes)
        if shape == "union":
            return f"({text}) + {pick(atoms, denials)}"
        if shape == "both":
            return f"({text}) & {pick(atoms, denials)}"
        if shape == "minus":
            return f"({text}) - {pick(denials, denials)}"
        if shape == "kept":
            return f"(({text}) - {pick(denials, denials)}) + {pick(atoms, denials)}"
        if shape == "swap":
            return f"{pick(atoms, denials)} + ({text})"
        return f"({text}) - ({pick(denials, denials)} - {pick(atoms, denials)})"

    grants = ["reader", "editor", "cav", "pub", "temp", "nil", "authenticated", "al1", "al2"]
    denials = ["banned", "muted", "cav"]
    every = [*directional, "kept"]
    p0 = pick(grants, denials)
    p1 = derived(p0, grants, denials, directional)
    p2 = derived(rng.choice(["p0", "p1", p0]), grants, denials, every)
    p3 = derived(rng.choice(["p1", "p2", p1]), [*grants, "p0"], denials, every)
    targets = ["p0", "p1", "p2", "p3"]
    edges = [*(f"{via}->{name}" for via in ("thread", "other") for name in targets), "direct"]
    edge_denials = ["banned", f"thread->{rng.choice(targets)}", f"other->{rng.choice(targets)}"]
    # The message's permissions have the thread's names: one goal, two types.
    m0 = f"{rng.choice(['thread', 'other'])}->{rng.choice(targets)} + direct"
    m1 = derived(m0, edges, edge_denials, every)
    m2 = derived(rng.choice(["p0", "p1", rng.choice(edges)]), edges, edge_denials, every)
    # ``r0`` is recursive; the others name it, as a permission, through ``parent``, or in a
    # subtrahend (``r3`` and ``r4`` exclude a recursion from one minuend).
    folder_atoms = ["viewer", "editor", "parent->r0", "r0", "nil"]
    r1 = rng.choice(["parent->r0", "parent->r0 + viewer", pick(folder_atoms, ["banned"])])
    r2 = derived(rng.choice(["r0", "r1", "parent->r0"]), folder_atoms, ["banned"], every)
    base = rng.choice(["editor", "editor + viewer", pick(["editor", "viewer"], ["banned"])])
    recursions = ["r0", "parent->r0", "(parent->r0 + viewer)", "banned"]
    r3 = f"({base}) - {rng.choice(recursions)}"
    r4 = f"({base}) - {rng.choice(recursions)}"
    schema = f"""
use expiration
caveat enabled(ok bool) {{ ok }}
definition auth/user {{}}
definition auth/group {{ relation member: auth/user }}
definition scope/thread {{
    relation reader: auth/user | auth/group#member
    relation editor: auth/user
    relation cav: auth/user with enabled
    relation pub: auth/user:*
    relation temp: auth/user with expiration
    relation banned: auth/user
    relation muted: auth/user | auth/group#member
    {"relation authenticated: auth/user" if conflicting and seed % 2 else "permission authenticated = editor"}
    {"relation al1: auth/user" if conflicting and not seed % 2 else ""}
    permission al1 = al2 + reader
    permission al2 = al1 + editor
    permission p0 = {p0}
    permission p1 = {p1}
    permission p2 = {p2}
    permission p3 = {p3}
}}
definition scope/message {{
    relation thread: scope/thread // rebac:field=thread
    relation other: scope/thread
    relation direct: auth/user
    relation banned: auth/user
    permission p0 = {m0}
    permission p1 = {m1}
    permission p2 = {m2}
}}
definition scope/folder {{
    relation parent: scope/folder
    relation viewer: auth/user
    relation editor: auth/user
    relation banned: auth/user
    permission r0 = parent->r0 + viewer
    permission r1 = {r1}
    permission r2 = {r2}
    permission r3 = {r3}
    permission r4 = {r4}
}}
"""
    now = timezone.now()
    tuples: list[RelationshipTuple] = []
    if rng.random() < 0.7:
        tuples.append(RelationshipTuple(ObjectRef("auth/group", "g"), "member", ACTORS[0]))
    names_read_two_ways = ("authenticated",) if seed % 2 else ("al1",)
    for oid in ("1", "2"):
        for relation in (
            "reader",
            "editor",
            "banned",
            "muted",
            *(names_read_two_ways if conflicting else ()),
        ):
            for actor in ACTORS:
                if rng.random() < 0.3:
                    tuples.append(
                        RelationshipTuple(ObjectRef("scope/thread", oid), relation, actor)
                    )
        for relation in ("reader", "muted"):
            if rng.random() < 0.3:
                tuples.append(RelationshipTuple(ObjectRef("scope/thread", oid), relation, GROUP))
        if rng.random() < 0.4:
            tuples.append(
                RelationshipTuple(
                    ObjectRef("scope/thread", oid),
                    "cav",
                    rng.choice(ACTORS),
                    caveat_name="enabled",
                    caveat_context=rng.choice([{"ok": True}, {"ok": False}, None]),
                )
            )
        if rng.random() < 0.3:
            tuples.append(
                RelationshipTuple(
                    ObjectRef("scope/thread", oid), "pub", SubjectRef.of("auth/user", "*")
                )
            )
        if rng.random() < 0.4:
            tuples.append(
                RelationshipTuple(
                    ObjectRef("scope/thread", oid),
                    "temp",
                    rng.choice(ACTORS),
                    expires_at=now + rng.choice([timedelta(hours=1), -timedelta(seconds=5)]),
                )
            )
    for oid in ("1", "2", "3"):
        for relation in ("direct", "banned"):
            if rng.random() < 0.3:
                tuples.append(
                    RelationshipTuple(ObjectRef("scope/message", oid), relation, rng.choice(ACTORS))
                )
        # The other thread of a message, which its own thread need not be.
        tuples.append(
            RelationshipTuple(
                ObjectRef("scope/message", oid),
                "other",
                SubjectRef.of("scope/thread", rng.choice(["1", "2"])),
            )
        )
    # A chain of four folders, the root's viewer the member: three hops, one past the limit.
    tuples.append(RelationshipTuple(ObjectRef("scope/folder", "f0"), "viewer", ACTORS[0]))
    for n in (1, 2, 3):
        tuples.append(
            RelationshipTuple(
                ObjectRef("scope/folder", f"f{n}"),
                "parent",
                SubjectRef.of("scope/folder", f"f{n - 1}"),
            )
        )
    # Grants on the chain's last folder and on a folder with no parent: a recursion cut at
    # the limit counts as holding there when it is read at the upper bound, in a subtrahend.
    tuples.append(RelationshipTuple(ObjectRef("scope/folder", "f3"), "editor", ACTORS[0]))
    tuples.append(RelationshipTuple(ObjectRef("scope/folder", "h0"), "editor", ACTORS[0]))
    for oid in ("f0", "f1", "f2", "f3", "h0"):
        for relation in ("viewer", "editor", "banned"):
            if rng.random() < 0.2:
                tuples.append(
                    RelationshipTuple(ObjectRef("scope/folder", oid), relation, rng.choice(ACTORS))
                )
    return schema, tuples


@pytest.mark.pg_delta
@pytest.mark.parametrize("seed", seeds(20, fast=3))
def test_the_edges_listed_are_the_edges_delivered(seed, settings):
    """``structural.edges()`` lists, for the policy in force, each foreign key structural delivery
    serves and the parent actions it serves it under. On the randomized policies, for each action a
    message is read under and each permission of the thread its parents were admitted under, a
    running delivery delivers a message query bound to the admitted thread exactly when the edge
    lists that parent action."""
    settings.REBAC_DEPTH_LIMIT = 2
    schema, tuples = implication_policy(seed)
    local = install_schema(schema)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1), ScopeThread(id=2)])
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=n, thread_id=1 if n < 3 else 2) for n in (1, 2, 3)]
    )
    if tuples:
        local.write_relationships(tuples)
    policy = structural.current_policy()
    thread = local.schema().get_definition("scope/thread")
    assert thread is not None
    actor = SubjectRef.of("auth/user", "member")
    [parent] = ScopeThread._base_manager.filter(pk=1)
    query = ScopeMessage.objects.filter(thread_id__in=[1]).query
    listed = 0
    for child_action in ("p0", "p1", "p2"):
        found = {
            (edge.child_model, edge.fk_name, edge.parent_model): edge
            for edge in structural.edges(child_action=child_action)
        }
        assert all(edge.child_action == child_action for edge in found.values())
        # The random policies back no other foreign key of the scope models.
        assert set(found) <= {(ScopeMessage, "thread", ScopeThread)}, set(found)
        edge = found.get((ScopeMessage, "thread", ScopeThread))
        served = set(edge.parent_actions) if edge is not None else set()
        listed += len(served)
        for permission in thread.permissions:
            with structural.delivering(actor, ScopeThread, permission.name, [parent]):
                delivered = structural.delivers(
                    query, ScopeMessage, actor, child_action, using="default"
                )
            assert delivered == (permission.name in served), (seed, child_action, permission.name)
            assert (
                structural.explain(
                    policy,
                    child_model=ScopeMessage,
                    fk_name="thread",
                    parent_model=ScopeThread,
                    child_action=child_action,
                    parent_action=permission.name,
                )
                is None
            ) == delivered
    # Not vacuous: 13 of the 20 seeds list an edge, the three of the default suite among them.
    if seed in (0, 1, 2):
        assert listed, seed


def test_the_public_forms_take_the_policy_in_force():
    """``current_policy()`` is the policy a delivery is decided under, and the proofs take it when
    none is passed. Without a local backend nothing is proven and nothing is listed."""
    from rebac.backends import reset_backend

    local = install_schema(POLICY.format(permissions=held_and_granted("viewer", "viewer")))
    policy = structural.current_policy()
    assert policy is not None and policy.key == read.policy_key(local)
    assert structural.current_policy() is policy
    edge = {
        "child_model": ScopeMessage,
        "fk_name": "thread",
        "parent_model": ScopeThread,
        "child_action": "read",
        "parent_action": "read",
    }
    assert structural.explain(**edge) == structural.explain(policy, **edge)
    assert structural.parent_implies_children(**edge) == structural.parent_implies_children(
        policy, **edge
    )
    assert structural.edges() == structural.edges(policy)
    reset_backend()


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("seed", seeds(60))
def test_every_proven_implication_holds_on_the_data(seed, storage, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    settings.REBAC_DEPTH_LIMIT = 2
    schema, tuples = implication_policy(seed)
    local = install_schema(schema)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1), ScopeThread(id=2)])
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=n, thread_id=1 if n < 3 else 2) for n in (1, 2, 3)]
    )
    if tuples:
        local.write_relationships(tuples)
    policy = read._policy(local)
    granted: dict[tuple[SubjectRef, str, str], set[str]] = {}

    def holders(actor: SubjectRef, type_: str, action: str) -> set[str]:
        key = (actor, type_, action)
        if key not in granted:
            granted[key] = set(local.accessible(subject=actor, action=action, resource_type=type_))
        return granted[key]

    proven = 0
    for type_, actions in (
        ("scope/thread", ("p0", "p1", "p2", "p3")),
        ("scope/message", ("p0", "p1", "p2")),
        ("scope/folder", ("r0", "r1", "r2", "r3", "r4")),
    ):
        for held, wanted in itertools.permutations(actions, 2):
            if not structural.implies(policy, type_, held, wanted):
                continue
            proven += 1
            for actor in ACTORS:
                assert holders(actor, type_, held) <= holders(actor, type_, wanted), (
                    seed,
                    type_,
                    held,
                    wanted,
                    actor,
                )
    # Each policy derives ``p1`` from ``p0`` by a shape the prover reads in one direction.
    assert proven, (seed, schema)
