"""Serializable caveat formulas; CEL always comes from the supplied schema."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from itertools import product
from typing import Any

from rebac.caveats import evaluate as evaluate_caveat
from rebac.conf import app_settings
from rebac.errors import SchemaError
from rebac.schema.ast import Schema
from rebac.schema.serialization import canonical_json, digest

type Formula = bool | dict[str, Any] | None


def leaf(caveat_name: str, pinned: Mapping[str, Any] | None) -> Formula:
    if not caveat_name:
        return None
    # Round-trip to detach mutable caller-owned context and reject non-JSON values.
    return {"caveat": caveat_name, "context": json.loads(canonical_json(dict(pinned or {})))}


def pinned(
    caveat_name: str,
    context: Mapping[str, Any] | None,
    schema: Schema,
    recaveated: Iterable[str] = (),
) -> Formula:
    """The condition a tuple's caveat contributes to the index.

    A caveat its pinned context decides is decided here: true stores the row
    without a condition and false stores no row, so scopes are exact for it.
    It stays a leaf when it needs runtime context, or when an override can
    redefine it, because its definition is then read when a check runs.
    """
    formula = leaf(caveat_name, context)
    if formula is None or caveat_name in recaveated:
        return formula
    verdict, _ = evaluate(formula, schema, None)
    if verdict is None:
        return formula
    return None if verdict else False


def _combine(operator: str, formulas: Iterable[Formula]) -> Formula:
    terms: list[Formula] = []
    seen: set[str] = set()
    for formula in formulas:
        if formula is None or formula is True:
            if operator == "or":
                return None
            continue
        if formula is False:
            if operator == "and":
                return False
            continue
        children = formula[operator] if operator in formula else [formula]
        for child in children:
            identity = key(child)
            if identity not in seen:
                seen.add(identity)
                terms.append(child)
    if not terms:
        return None if operator == "and" else False
    if len(terms) == 1:
        return terms[0]
    # Keep evaluation order. In particular do not absorb a | (a & b).
    return {operator: terms}


def and_(*fs: Formula) -> Formula:
    return _combine("and", fs)


def or_(*fs: Formula) -> Formula:
    return _combine("or", fs)


def key(f: Formula) -> str:
    return "" if f is None else digest(f)


def size(f: Formula) -> int:
    if not isinstance(f, dict):
        return 0
    if "caveat" in f:
        return 1
    children = f.get("and") if "and" in f else f.get("or")
    if not isinstance(children, list):
        return 0
    total = 0
    for child in children:
        total += size(child)
    return total


def enforce_limit(formulas: Iterable[Formula], *, label: str) -> None:
    """Bound a complete contribution, including alternative condition-key rows."""
    contribution = or_(*formulas)
    enforce_size(size(contribution), label=label)


def enforce_size(count: int, *, label: str) -> None:
    """Check an already-normalized contribution with distinct holder predicates."""
    limit = app_settings.REBAC_INDEX_CONDITION_LIMIT
    if count > limit:
        raise SchemaError(
            f"Permission index condition for {label} has {count} caveat instances "
            f"(REBAC_INDEX_CONDITION_LIMIT={limit})"
        )


type Path = frozenset[str]
type Verdict = tuple[bool | None, frozenset[str]]


class Evaluation:
    """Three-valued results for one read, with the parameters still needed.

    A node holds through alternative paths, and a path holds when all of its
    atoms do. An atom is a caveat instance, or a site a caller registers with
    its own verdict. When the result is conditional, the missing parameters
    are those of the atoms the result still depends on: a path that cannot
    hold contributes none, and neither does a path that needs everything a
    shorter one needs. The set does not depend on the order of paths or rows.
    """

    def __init__(self, schema: Schema, context: Mapping[str, Any] | None) -> None:
        self.schema = schema
        self.context = dict(context or {})
        self.atoms: dict[str, Verdict] = {}

    def paths(self, formula: Formula) -> list[Path]:
        """The alternative sets of atoms under which a stored formula holds."""
        if formula is None or formula is True:
            return [frozenset()]
        if formula is False:
            return []
        if "caveat" in formula:
            atom = key(formula)
            if atom not in self.atoms:
                caveat = self.schema.get_caveat(formula["caveat"])
                if caveat is None:
                    self.atoms[atom] = False, frozenset()
                else:
                    verdict, names = evaluate_caveat(caveat, formula["context"], self.context)
                    self.atoms[atom] = verdict, frozenset(names)
            return [frozenset({atom})]
        if "or" in formula:
            return [path for child in formula["or"] for path in self.paths(child)]
        alternatives = [self.paths(child) for child in formula["and"]]
        return [frozenset().union(*parts) for parts in product(*alternatives)]

    def decide(self, paths: Iterable[Path]) -> Verdict:
        """Whether any path holds, and what is missing to know."""
        open_paths: set[Path] = set()
        for path in paths:
            verdicts = [self.atoms[atom][0] for atom in path]
            if False in verdicts:
                continue
            unknown = frozenset(atom for atom in path if self.atoms[atom][0] is None)
            if not unknown:
                return True, frozenset()
            open_paths.add(unknown)
        needed = [path for path in open_paths if not any(other < path for other in open_paths)]
        if not needed:
            return False, frozenset()
        return None, frozenset().union(*(self.atoms[atom][1] for path in needed for atom in path))


def evaluate(f: Formula, schema: Schema, context: Mapping[str, Any] | None) -> Verdict:
    evaluation = Evaluation(schema, context)
    return evaluation.decide(evaluation.paths(f))
