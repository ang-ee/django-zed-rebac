"""SchemaOverride composition -- Tier-2 layer over the Tier-1 baseline.

Pure-AST composition function::

    effective_expr = (((baseline_expr U extends) MINUS disables) AND tightens)

with caveats merged from `recaveats`. Identity elements:

  - `extends = empty`   -> `X U {} = X`
  - `disables = empty`  -> `X - {} = X`
  - `tightens = U`      -> `X & U = X`
  - `recaveats = empty` -> baseline caveat unchanged

So zero overrides reduces to an AST-equal copy of the baseline.

Union and intersection are commutative + associative. Subtraction is NOT --
multiple `disables` rows are applied deterministically in
`(kind, created_at, pk)` order.

Operates on ``rebac.schema.ast`` nodes -- never strings. Caveat evaluation
happens at row evaluation time (LocalBackend), not here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .errors import SchemaError
from .schema.ast import (
    Caveat,
    Definition,
    PermArrow,
    PermBinOp,
    PermExpr,
    Permission,
    PermNil,
    PermRef,
    Schema,
)
from .schema.parser import parse_permission_expression, reference_issues

if TYPE_CHECKING:
    from .models import SchemaOverride

__all__ = ["compose"]


@dataclass(frozen=True)
class ArmTag:
    deadline: datetime | None
    override: str


@dataclass(frozen=True)
class TaggedComposition:
    schema: Schema
    arms: dict[int, ArmTag]
    sites: dict[int, ArmTag]


# ---------- Public entry point ----------


def compose(baseline: Schema, overrides: Iterable[SchemaOverride]) -> Schema:
    return compose_tagged(baseline, overrides).schema


def compose_tagged(baseline: Schema, overrides: Iterable[SchemaOverride]) -> TaggedComposition:
    """Apply override rows to a baseline schema, returning a new Schema.

    Pure: does NOT mutate ``baseline``. The returned Schema has fresh
    AST nodes for any permission / caveat that was modified; unmodified
    definitions / caveats are returned by reference (cheap clone).

    Raises ``SchemaError`` if an override would introduce a permission cycle
    not present in the baseline.
    """
    rows = list(overrides)
    arms: dict[int, ArmTag] = {}
    sites: dict[int, ArmTag] = {}
    if not rows:
        # Identity: return a fresh Schema wrapping the baseline's tuples by
        # reference. Definitions / caveats are frozen dataclasses so sharing
        # them is safe.
        return TaggedComposition(
            Schema(
                definitions=list(baseline.definitions),
                caveats=list(baseline.caveats),
                directives=list(baseline.directives),
                headers=dict(baseline.headers),
            ),
            arms,
            sites,
        )

    perm_groups, caveat_groups = _group_overrides(rows)

    # Compose permissions per definition.
    new_definitions: list[Definition] = []
    for definition in baseline.definitions:
        new_perms: list[Permission] = []
        changed = False
        for perm in definition.permissions:
            ovs = perm_groups.get(("permission", definition.resource_type, perm.name))
            if not ovs:
                new_perms.append(perm)
                continue
            new_perm = _compose_permission(perm, ovs, arms=arms, sites=sites)
            new_perms.append(new_perm)
            changed = True
        if changed:
            new_definitions.append(
                Definition(
                    resource_type=definition.resource_type,
                    relations=definition.relations,
                    permissions=tuple(new_perms),
                )
            )
        else:
            new_definitions.append(definition)

    # Compose caveats.
    new_caveats: list[Caveat] = []
    for caveat in baseline.caveats:
        ovs = caveat_groups.get(caveat.name)
        if not ovs:
            new_caveats.append(caveat)
            continue
        new_caveats.append(_compose_caveat(caveat, ovs))

    composed = Schema(
        definitions=new_definitions,
        caveats=new_caveats,
        directives=list(baseline.directives),
        headers=dict(baseline.headers),
    )

    # Reject any composition that introduces a permission cycle that wasn't
    # present in the baseline.
    _detect_cycles(baseline, composed)
    baseline_errors = Counter(reference_issues(baseline))
    reference_errors = [
        issue.message()
        for issue in (Counter(reference_issues(composed)) - baseline_errors).elements()
    ]
    if reference_errors:
        raise SchemaError("; ".join(reference_errors))

    return TaggedComposition(composed, arms, sites)


def recaveat_targets(overrides: Iterable[SchemaOverride]) -> frozenset[str]:
    """The names of the caveats that recaveat overrides redefine."""
    rows = list(overrides)
    return frozenset(_group_overrides(rows)[1]) if rows else frozenset()


# ---------- Grouping ----------


def _group_overrides(
    rows: list[SchemaOverride],
) -> tuple[
    dict[tuple[str, str, str], list[SchemaOverride]],
    dict[str, list[SchemaOverride]],
]:
    """Bucket overrides by their target.

    Returns two dicts:
      - permission overrides keyed by ("permission", resource_type, perm_name)
      - caveat overrides keyed by caveat name

    Targets that point at unknown ContentType labels are dropped silently --
    the migration ships its own validation; here we just refuse to crash if
    an admin row points at a model from an uninstalled app.
    """
    perm_groups: dict[tuple[str, str, str], list[SchemaOverride]] = {}
    caveat_groups: dict[str, list[SchemaOverride]] = {}

    # Local imports avoid pulling Django models at module import time.
    from django.contrib.contenttypes.models import ContentType

    from .models import SchemaCaveat, SchemaPermission

    perm_ct_label = ("rebac", "schemapermission")
    caveat_ct_label = ("rebac", "schemacaveat")

    # Pre-resolve target rows in batches so we don't issue per-override queries.
    perm_pks: set[int] = set()
    caveat_pks: set[int] = set()
    for row in rows:
        try:
            label = (row.target_ct.app_label, row.target_ct.model)
        except ContentType.DoesNotExist:
            # Stale FK to a removed app/content type. Drop silently;
            # the override was unreachable anyway.
            continue
        if label == perm_ct_label:
            perm_pks.add(row.target_pk)
        elif label == caveat_ct_label:
            caveat_pks.add(row.target_pk)

    perm_lookup: dict[int, tuple[str, str]] = {}
    using = rows[0]._state.db if rows else None
    if perm_pks:
        for sp in (
            SchemaPermission.objects.using(using)
            .filter(pk__in=perm_pks)
            .select_related("definition")
        ):
            perm_lookup[sp.pk] = (sp.definition.resource_type, sp.name)

    caveat_lookup: dict[int, str] = {}
    if caveat_pks:
        for sc in SchemaCaveat.objects.using(using).filter(pk__in=caveat_pks):
            caveat_lookup[sc.pk] = sc.name

    for row in rows:
        try:
            label = (row.target_ct.app_label, row.target_ct.model)
        except ContentType.DoesNotExist:
            # Stale FK to a removed app/content type. Drop silently;
            # the override was unreachable anyway.
            continue
        if label == perm_ct_label:
            target = perm_lookup.get(row.target_pk)
            if target is None:
                continue
            key = ("permission", target[0], target[1])
            perm_groups.setdefault(key, []).append(row)
        elif label == caveat_ct_label:
            name = caveat_lookup.get(row.target_pk)
            if name is None:
                continue
            caveat_groups.setdefault(name, []).append(row)

    return perm_groups, caveat_groups


# ---------- Permission composition ----------


def _compose_permission(
    baseline_perm: Permission,
    overrides: list[SchemaOverride],
    *,
    arms: dict[int, ArmTag],
    sites: dict[int, ArmTag],
) -> Permission:
    """Compose a single permission row.

    Order: (((baseline U extends) - disables) & tightens)

    `extends` and `loosen` are unioned (they're aliases per PERMISSIONS.md
    section 4). `disables` are applied in (created_at, pk) order --
    subtraction is non-commutative, so determinism matters.
    """
    from .models import SchemaOverride as Ovr

    # Bucket and sort each kind by (created_at, pk) for full determinism even
    # when multiple rows share a timestamp (which happens on SQLite with its
    # second-resolution `auto_now_add`).
    sort_key = lambda r: (r.created_at, r.pk)  # noqa: E731

    extend_rows = sorted(
        [r for r in overrides if r.kind in (Ovr.KIND_EXTEND, Ovr.KIND_LOOSEN)],
        key=sort_key,
    )
    disable_rows = sorted(
        [r for r in overrides if r.kind == Ovr.KIND_DISABLE],
        key=sort_key,
    )
    tighten_rows = sorted(
        [r for r in overrides if r.kind == Ovr.KIND_TIGHTEN],
        key=sort_key,
    )

    expr: PermExpr = baseline_perm.expression

    # 1. baseline U extends (associative + commutative -- order doesn't change
    # the AST shape's truth value but we sort for byte-deterministic output).
    for r in extend_rows:
        arm = _parse_expr(r.expression, baseline_perm.name)
        arms[id(arm)] = ArmTag(r.expires_at, f"{r.kind}:{r.pk}")
        expr = PermBinOp("+", expr, arm)

    # 2. - disables (NON-commutative).
    for r in disable_rows:
        expr = PermBinOp("-", expr, _parse_expr(r.expression, baseline_perm.name))
        sites[id(expr)] = ArmTag(r.expires_at, f"{r.kind}:{r.pk}")

    # 3. & tightens.
    for r in tighten_rows:
        expr = PermBinOp("&", expr, _parse_expr(r.expression, baseline_perm.name))
        sites[id(expr)] = ArmTag(r.expires_at, f"{r.kind}:{r.pk}")

    return Permission(
        name=baseline_perm.name,
        expression=expr,
        raw_text=baseline_perm.raw_text,
    )


def _parse_expr(text: str, perm_name: str) -> PermExpr:
    try:
        return parse_permission_expression(text)
    except Exception as exc:
        raise SchemaError(
            f"Override on permission {perm_name!r} has invalid expression {text!r}: {exc}"
        ) from exc


# ---------- Caveat composition ----------


def _compose_caveat(baseline_caveat: Caveat, overrides: list[SchemaOverride]) -> Caveat:
    """Compose a caveat -- last-by-`created_at` RECAVEAT wins.

    Override params replacement is OUT OF SCOPE for v1; we leave the param
    list unchanged. If a future RECAVEAT format wants to ship a new param
    list, add a parser in v1.x.
    """
    from .models import SchemaOverride as Ovr

    recaveats = [r for r in overrides if r.kind == Ovr.KIND_RECAVEAT]
    if not recaveats:
        return baseline_caveat

    # Last write wins (strict by created_at, then PK as deterministic tiebreak).
    recaveats.sort(key=lambda r: (r.created_at, r.pk))
    winner = recaveats[-1]

    return Caveat(
        name=baseline_caveat.name,
        params=baseline_caveat.params,
        expression=winner.expression,
    )


# ---------- Cycle detection ----------


def _detect_cycles(baseline: Schema, composed: Schema) -> None:
    """Reject overrides that introduce permission cycles not in the baseline.

    A "cycle" here is a chain of PermRef references between permissions on
    the same Definition that closes back on itself. `read = read` is the
    trivial case; `read = a; a = read` is the two-hop case.

    Simple DFS per definition; bounded by definition size.
    """
    for new_def in composed.definitions:
        baseline_def = baseline.get_definition(new_def.resource_type)
        baseline_cycles = _cycles_in_definition(baseline_def) if baseline_def else set()
        new_cycles = _cycles_in_definition(new_def)
        introduced = new_cycles - baseline_cycles
        if introduced:
            sample = sorted(introduced)[0]
            raise SchemaError(
                f"Override would introduce permission cycle: {new_def.resource_type}#{sample}"
            )


def _cycles_in_definition(definition: Definition) -> set[str]:
    """Return the set of permission names that participate in a cycle.

    Walks PermRef edges only between permissions on the same Definition; a
    PermRef pointing at a relation terminates that branch. PermArrow does
    not contribute (arrows route to other definitions and are validated by
    the schema doctor).
    """
    perm_names = {p.name for p in definition.permissions}
    if not perm_names:
        return set()

    edges: dict[str, set[str]] = {}
    for perm in definition.permissions:
        edges[perm.name] = _refs_in_expr(perm.expression) & perm_names

    from .schema.graph import strongly_connected_components

    return {
        name
        for component in strongly_connected_components(edges)
        if len(component) > 1 or any(name in edges[name] for name in component)
        for name in component
    }


def _refs_in_expr(expr: PermExpr) -> set[str]:
    """Collect all PermRef names. Arrows and Nil don't contribute."""
    if isinstance(expr, PermNil):
        return set()
    if isinstance(expr, PermRef):
        return {expr.name}
    if isinstance(expr, PermArrow):
        return set()
    if isinstance(expr, PermBinOp):
        return _refs_in_expr(expr.left) | _refs_in_expr(expr.right)
    return set()
