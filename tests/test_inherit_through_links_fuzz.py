"""The narrowing of ``REBAC_INHERIT_THROUGH_LINKS`` against a set-semantics reference (proposal 0023).

No database: random policies of two to four types, random classifications of
their relations (soundness must not depend on how a relation is classified),
random relationship rows, and overrides modelled as ``compose_tagged`` tags
them, with one deadline. A reference evaluator computes every permission on
every object before the deadline and after it, for the published policy and
for the narrowed one, and the test asserts:

- every declared permission of the narrowed policy grants a subset of the
  published one;
- ``lent(P)`` grants a subset of ``P`` and ``excluded(P)``, ``excluded(lent(P))``
  a superset, as published (what makes an exclusion take away at least as much);
- every generated name is one no policy can declare.

A variant names one permission ``authenticated``: inside an expression that name is
the actor's state, as the compiler reads it (``rebac.schema.ast.is_builtin_reference``),
and only an arrow leads to the permission. Another lets a permission take a relation's
name. Where a policy reads a name two ways (``rebac.schema.ast.name_conflicts``: a
relation named as a built-in, a name declared twice) the narrowing must refuse it.

The default suite runs a fixed list of seeds; the slow tier thousands.
"""

from __future__ import annotations

import random
from itertools import count
from typing import Any

import pytest

from rebac import links
from rebac.composition import ArmTag, TaggedComposition
from rebac.schema.ast import (
    AllowedSubject,
    Definition,
    PermArrow,
    PermBinOp,
    PermExpr,
    Permission,
    PermNil,
    PermRef,
    Relation,
    Schema,
    is_builtin_reference,
    schema_name_conflicts,
)

USERS = ("u1", "u2", "u3")
IDS = ("1", "2")
KINDS = (links.PARENT, links.CONSTANT, links.SAME_TYPE, links.MEMBERSHIP, links.LINK)
NAMES = ("p", "q", "s")
# A permission named as a built-in: inside an expression the name is the actor's state.
BUILTIN = ("p", "q", "authenticated")
TAG = ArmTag(None, "fuzz")

type Row = tuple[str, str, str, str, str, str]


class Skip(Exception):
    """The published policy recurses through an exclusion: the compiler refuses it too."""


def make_schema(
    rng: random.Random, names: tuple[str, ...] = NAMES, conflicting: bool = False
) -> Schema:
    types = [f"t/{c}" for c in "abcd"[: rng.randint(2, 4)]]
    relations: dict[str, list[Relation]] = {}
    for type_ in types:
        own = [
            Relation("r1", (AllowedSubject("user"),)),
            Relation("r2", (AllowedSubject("user"), AllowedSubject("user", wildcard=True))),
        ]
        if rng.random() < 0.5:
            own.append(
                Relation("team", (AllowedSubject("user"), AllowedSubject(rng.choice(types), "r1")))
            )
        for index in range(rng.randint(1, 3)):
            subjects = rng.sample(types, rng.randint(1, min(3, len(types))))
            own.append(Relation(f"v{index}", tuple(AllowedSubject(s) for s in subjects)))
        if rng.random() < 0.3:
            # An arrow target that is a relation on this type and a permission on others.
            own.append(Relation(rng.choice(names), (AllowedSubject("user"),)))
        relations[type_] = own
    declared: dict[str, list[str]] = {}
    for type_ in types:
        taken = {relation.name for relation in relations[type_]}
        # ``conflicting``: a permission may take a relation's name, which reads it two ways.
        mine = [name for name in names if conflicting or name not in taken]
        rng.shuffle(mine)
        declared[type_] = mine[: rng.randint(1, len(mine))] if mine else []
    definitions = []
    for type_ in types:
        vias = [r.name for r in relations[type_] if r.name.startswith("v")]
        plain = [r.name for r in relations[type_] if not r.name.startswith("v")]
        permissions = []
        for index, name in enumerate(declared[type_]):
            earlier = declared[type_][:index]

            def leaf(
                earlier: list[str] = earlier, vias: list[str] = vias, plain: list[str] = plain
            ) -> PermExpr:
                roll = rng.random()
                if roll < 0.3:
                    return PermRef(rng.choice(plain))
                if roll < 0.4 and earlier:
                    return PermRef(rng.choice(earlier))
                return PermArrow(rng.choice(vias), rng.choice([*names, "r1"]))

            def tree(depth: int, leaf: Any = leaf) -> PermExpr:
                if depth == 0 or rng.random() < 0.3:
                    return leaf()
                op = rng.choice(["+", "+", "-", "-", "&"])
                return PermBinOp(op, tree(depth - 1), tree(depth - 1))

            permissions.append(Permission(name, tree(rng.randint(1, 4))))
        definitions.append(Definition(type_, tuple(relations[type_]), tuple(permissions)))
    return Schema(definitions=definitions, caveats=[], directives=[], headers={})


def make_rows(rng: random.Random, schema: Schema) -> set[Row]:
    rows: set[Row] = set()
    for definition in schema.definitions:
        for object_id in IDS:
            for relation in definition.relations:
                for subject in relation.allowed_subjects:
                    if subject.type == "user" and subject.wildcard:
                        if rng.random() < 0.1:
                            rows.add(
                                (
                                    definition.resource_type,
                                    object_id,
                                    relation.name,
                                    "user",
                                    "*",
                                    "",
                                )
                            )
                    elif subject.type == "user":
                        for user in USERS:
                            if rng.random() < 0.3:
                                rows.add(
                                    (
                                        definition.resource_type,
                                        object_id,
                                        relation.name,
                                        "user",
                                        user,
                                        "",
                                    )
                                )
                    else:
                        for target in IDS:
                            if rng.random() < 0.4:
                                rows.add(
                                    (
                                        definition.resource_type,
                                        object_id,
                                        relation.name,
                                        subject.type,
                                        target,
                                        subject.relation,
                                    )
                                )
    return rows


def classifier(seed: int, parent_links: str) -> Any:
    """A random classification; ``parent_links`` is ``"real"`` (only parents) or ``"any"``."""

    def classify(definition: Definition, relation: Relation) -> links.Classification:
        rng = random.Random(f"{seed}:{definition.resource_type}:{relation.name}")
        kind = rng.choice(KINDS)
        if parent_links == "any":
            link = rng.random() < 0.3
        else:
            link = kind == links.PARENT and rng.random() < 0.5
        return links.Classification(kind, "fuzz", parent_link=link)

    return classify


def add_overrides(
    rng: random.Random, schema: Schema, names: tuple[str, ...] = NAMES
) -> TaggedComposition:
    """Wrap some permissions as ``compose_tagged`` does: ``((P + arm) - D) & T``, tagged."""
    arms: dict[int, ArmTag] = {}
    sites: dict[int, ArmTag] = {}
    definitions = []
    for definition in schema.definitions:
        vias = [r.name for r in definition.relations if r.name.startswith("v")]
        targets = [*names, "r1"]
        permissions = []
        for permission in definition.permissions:
            expression = permission.expression
            if rng.random() < 0.5:
                arm: PermExpr = PermArrow(rng.choice(vias), rng.choice(targets))
                if rng.random() < 0.3:
                    arm = PermBinOp("+", PermRef("r1"), arm)
                arms[id(arm)] = TAG
                expression = PermBinOp("+", expression, arm)
            if rng.random() < 0.4:
                expression = PermBinOp(
                    "-", expression, PermArrow(rng.choice(vias), rng.choice(targets))
                )
                sites[id(expression)] = TAG
            if rng.random() < 0.3:
                expression = PermBinOp(
                    "&",
                    expression,
                    PermBinOp("+", PermRef("r2"), PermArrow(rng.choice(vias), rng.choice(targets))),
                )
                sites[id(expression)] = TAG
            permissions.append(Permission(permission.name, expression))
        definitions.append(
            Definition(definition.resource_type, definition.relations, tuple(permissions))
        )
    return TaggedComposition(
        Schema(definitions=definitions, caveats=[], directives=[], headers={}), arms, sites
    )


class Evaluator:
    """Every relation and permission on every object, by fixpoint, before or after the deadline."""

    def __init__(self, tagged: TaggedComposition, rows: set[Row], *, after: bool) -> None:
        self.schema = tagged.schema
        self.arms, self.sites, self.after = tagged.arms, tagged.sites, after
        self.definitions = {d.resource_type: d for d in self.schema.definitions}
        self.rows = rows
        self.relation: dict[tuple[str, str, str], frozenset[str]] = {}
        self.permission: dict[tuple[str, str, str], frozenset[str]] = {}
        self._relations()
        self._permissions()

    def is_relation(self, type_: str, name: str) -> bool:
        definition = self.definitions.get(type_)
        return definition is not None and any(r.name == name for r in definition.relations)

    def is_permission(self, type_: str, name: str) -> bool:
        definition = self.definitions.get(type_)
        return definition is not None and any(p.name == name for p in definition.permissions)

    def _relations(self) -> None:
        for definition in self.schema.definitions:
            for relation in definition.relations:
                for object_id in IDS:
                    self.relation[(definition.resource_type, object_id, relation.name)] = (
                        frozenset()
                    )
        changed = True
        while changed:
            changed = False
            for type_, object_id, name in list(self.relation):
                found: set[str] = set()
                for (
                    row_type,
                    row_id,
                    row_relation,
                    subject_type,
                    subject_id,
                    subject_relation,
                ) in self.rows:
                    if (row_type, row_id, row_relation) != (type_, object_id, name):
                        continue
                    if subject_type == "user" and not subject_relation:
                        found |= set(USERS) if subject_id == "*" else {subject_id}
                    elif subject_relation:
                        found |= self.relation.get(
                            (subject_type, subject_id, subject_relation), frozenset()
                        )
                value = frozenset(found)
                if value != self.relation[(type_, object_id, name)]:
                    self.relation[(type_, object_id, name)] = value
                    changed = True

    def value(self, type_: str, object_id: str, name: str) -> frozenset[str]:
        if self.is_permission(type_, name):
            return self.permission.get((type_, object_id, name), frozenset())
        if self.is_relation(type_, name):
            return self.relation[(type_, object_id, name)]
        return frozenset()

    def expression(self, type_: str, object_id: str, expr: PermExpr) -> frozenset[str]:
        if self.after:
            if id(expr) in self.arms:
                return frozenset()
            if id(expr) in self.sites:
                assert isinstance(expr, PermBinOp)
                return self.expression(type_, object_id, expr.left)
        if isinstance(expr, PermNil):
            return frozenset()
        if isinstance(expr, PermRef):
            if is_builtin_reference(expr.name):
                # The actor's state: every user here is authenticated.
                return frozenset(USERS) if expr.name == "authenticated" else frozenset()
            return self.value(type_, object_id, expr.name)
        if isinstance(expr, PermArrow):
            found: set[str] = set()
            for (
                row_type,
                row_id,
                row_relation,
                subject_type,
                subject_id,
                subject_relation,
            ) in self.rows:
                if (row_type, row_id, row_relation) == (type_, object_id, expr.via) and (
                    subject_type != "user" and not subject_relation
                ):
                    found |= self.value(subject_type, subject_id, expr.target)
            return frozenset(found)
        left = self.expression(type_, object_id, expr.left)
        right = self.expression(type_, object_id, expr.right)
        if expr.op == "+":
            return left | right
        if expr.op == "&":
            return left & right
        return left - right

    def _dependencies(
        self, type_: str, expr: PermExpr, negative: bool = False
    ) -> list[tuple[tuple[str, str], bool]]:
        if isinstance(expr, PermRef):
            if is_builtin_reference(expr.name) or not self.is_permission(type_, expr.name):
                return []
            return [((type_, expr.name), negative)]
        if isinstance(expr, PermArrow):
            relation = next(
                (r for r in self.definitions[type_].relations if r.name == expr.via), None
            )
            if relation is None:
                return []
            return [
                ((s.type, expr.target), negative)
                for s in relation.allowed_subjects
                if self.is_permission(s.type, expr.target)
            ]
        if isinstance(expr, PermBinOp):
            return self._dependencies(type_, expr.left, negative) + self._dependencies(
                type_, expr.right, negative ^ (expr.op == "-")
            )
        return []

    def _permissions(self) -> None:
        expressions = {
            (d.resource_type, p.name): p.expression
            for d in self.schema.definitions
            for p in d.permissions
        }
        graph = {key: self._dependencies(key[0], expr) for key, expr in expressions.items()}
        index: dict[tuple[str, str], int] = {}
        low: dict[tuple[str, str], int] = {}
        stack: list[tuple[str, str]] = []
        on: set[tuple[str, str]] = set()
        components: list[list[tuple[str, str]]] = []
        counter = count()

        def strong(node: tuple[str, str]) -> None:
            index[node] = low[node] = next(counter)
            stack.append(node)
            on.add(node)
            for target, _negative in graph[node]:
                if target not in index:
                    strong(target)
                    low[node] = min(low[node], low[target])
                elif target in on:
                    low[node] = min(low[node], index[target])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)

        for node in expressions:
            if node not in index:
                strong(node)
        for component in components:
            members = set(component)
            if any(
                target in members and negative
                for node in component
                for target, negative in graph[node]
            ):
                raise Skip(component)
            for type_, name in component:
                for object_id in IDS:
                    self.permission[(type_, object_id, name)] = frozenset()
            changed = True
            while changed:
                changed = False
                for type_, name in component:
                    for object_id in IDS:
                        value = self.expression(type_, object_id, expressions[(type_, name)])
                        if value != self.permission[(type_, object_id, name)]:
                            self.permission[(type_, object_id, name)] = value
                            changed = True


def violations(
    seed: int,
    parent_links: str,
    overrides: bool,
    monkeypatch: Any,
    names: tuple[str, ...] = NAMES,
    conflicting: bool = False,
) -> list[Any] | None:
    """What the narrowing of one random policy gets wrong; ``None`` when the policy is refused."""
    rng = random.Random(seed)
    schema = make_schema(rng, names, conflicting)
    rows = make_rows(rng, schema)
    monkeypatch.setattr(links, "classify", classifier(seed, parent_links))
    tagged = add_overrides(rng, schema, names) if overrides else TaggedComposition(schema, {}, {})
    try:
        published = [Evaluator(tagged, rows, after=after) for after in (False, True)]
    except Skip:
        return None
    try:
        narrowed = (
            links.narrowed_tagged(tagged)
            if overrides
            else TaggedComposition(links.narrowed(schema), {}, {})
        )
    except links.NarrowingError as exc:
        # The one refusal these policies can meet: a name read two ways (a relation named as
        # a built-in, a name declared as a relation and a permission). Kept: every decision
        # fails closed.
        if exc.kept and schema_name_conflicts(schema):
            return None
        raise
    if schema_name_conflicts(schema):
        return [("a policy that reads a name two ways was narrowed", schema_name_conflicts(schema))]
    try:
        evaluated = [Evaluator(narrowed, rows, after=after) for after in (False, True)]
    except Skip as exc:
        # The published policy evaluates; the narrowed one must too.
        return [("the narrowed policy recurses through an exclusion", exc.args)]
    found: list[Any] = []
    declared = {(d.resource_type, p.name) for d in schema.definitions for p in d.permissions}
    for when, whole_policy, narrow_policy in zip(
        ("before", "after"), published, evaluated, strict=True
    ):
        for type_, name in declared:
            for object_id in IDS:
                whole = whole_policy.permission[(type_, object_id, name)]
                narrow = narrow_policy.permission[(type_, object_id, name)]
                if not narrow <= whole:
                    found.append((when, type_, object_id, name, sorted(whole), sorted(narrow)))
        for definition in narrowed.schema.definitions:
            for permission in definition.permissions:
                parts = links.generated_parts(permission.name)
                if (definition.resource_type, permission.name) in declared:
                    continue
                if parts is None or not links.is_generated(permission.name):
                    found.append(("undeclarable", definition.resource_type, permission.name))
                    continue
                _lent, negative, base = parts
                for object_id in IDS:
                    whole = whole_policy.value(definition.resource_type, object_id, base)
                    version = narrow_policy.permission[
                        (definition.resource_type, object_id, permission.name)
                    ]
                    # Under an exclusion a superset of the published permission, else a subset.
                    if not (version >= whole if negative else version <= whole):
                        found.append((when, definition.resource_type, object_id, permission.name))
    return found


def seeds(fast: range, slow: range) -> list[Any]:
    return [*fast, *(pytest.param(seed, marks=pytest.mark.slow) for seed in slow)]


CHUNK = 500


VARIANTS = {"declared": (NAMES, False), "builtin": (BUILTIN, False), "conflicting": (NAMES, True)}


@pytest.mark.parametrize("variant", sorted(VARIANTS))
@pytest.mark.parametrize("overrides", [False, True], ids=["plain", "overrides"])
@pytest.mark.parametrize("parent_links", ["real", "any"])
@pytest.mark.parametrize("chunk", seeds(range(0, 2), range(2, 40)))
def test_the_narrowing_grants_a_subset_on_random_policies(
    chunk, parent_links, overrides, variant, monkeypatch
):
    """500 policies per chunk, about a second; the default suite runs 2 chunks of each of
    the twelve variants (12,000 policies), the slow tier 38 more of each (228,000).

    Most random policies recurse through an exclusion, which the compiler refuses as
    well; a chunk evaluates about 180 of them without overrides and 75 with. Where a policy
    reads a name two ways (a relation named as a built-in, a name declared as a relation and a
    permission: ``conflicting`` lets a permission take a relation's name) the narrowing must
    refuse it, kept.
    """
    names, conflicting = VARIANTS[variant]
    evaluated = refused = 0
    for seed in range(chunk * CHUNK, (chunk + 1) * CHUNK):
        found = violations(seed, parent_links, overrides, monkeypatch, names, conflicting)
        if found is None:
            refused += 1
            continue
        evaluated += 1
        assert not found, (seed, parent_links, overrides, found[:5])
    assert evaluated >= (10 if conflicting else 25)
    # The variants that can read a name two ways do, on some of their policies.
    conflicted = sum(
        bool(schema_name_conflicts(make_schema(random.Random(seed), names, conflicting)))
        for seed in range(chunk * CHUNK, (chunk + 1) * CHUNK)
    )
    assert (conflicted > 0) is (variant != "declared"), (variant, conflicted, refused)
