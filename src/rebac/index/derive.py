"""Set-based monotone membership and grant derivation."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from time import perf_counter
from typing import Any

from django.db.models import DateTimeField, F, FilteredRelation, Max, OuterRef, Q, QuerySet, Value
from django.db.models.functions import Least

from rebac.models.index import IndexCover, IndexEdge, IndexMember, IndexTerm, IndexWork
from rebac.schema.ast import ConstBinding, PermArrow, PermBinOp, PermExpr, PermNil, PermRef

from . import conditions
from . import time as index_time
from .program import IndexProgram, Key, NodeSpec
from .project import (
    BATCH_SIZE,
    Stats,
    _term_id,
    formula_filter,
    formula_value,
    region_types,
    restrict,
    select,
    text,
    upsert,
)
from .terms import AUTHENTICATED, anonymous, intern, type_level

MEMBER_KEY = ("member_id", "set_id", "condition_key")
COVER_KEY = ("scope_id", "node", "holder_id", "site", "condition_key")
logger = logging.getLogger("rebac.index")


@contextmanager
def _pass(using: str) -> Iterator[int]:
    marker = IndexWork.objects.using(using).create(pass_id=0, kind="derivation", phase="new")
    completed = False
    try:
        yield marker.pk
        completed = True
    finally:
        if completed:
            IndexWork.objects.using(using).filter(Q(pass_id=marker.pk) | Q(pk=marker.pk)).delete()


def _members(
    source: QuerySet[Any],
    *,
    using: str,
    stats: Stats,
    pass_id: int,
    round_: int,
    region: int | None,
) -> int:
    rows = restrict(source, "set_id", using=using, region=region)
    return upsert(
        rows, IndexMember, MEMBER_KEY, using=using, stats=stats, pass_id=pass_id, round_=round_
    )


def _membership_seed(
    program: IndexProgram,
    *,
    using: str,
    region: int | None,
    stats: Stats,
    pass_id: int,
) -> int:
    usersets = Q(pk__in=[])
    for resource_type, name in sorted(program.userset_relations):
        usersets |= Q(resource_type=resource_type, relation=name)
    edges = IndexEdge.objects.using(using).filter(usersets).exclude(resource__relation="$type")
    projected = select(
        edges,
        member_id=F("subject_id"),
        member_type=F("subject__type"),
        set_id=_term_id(
            OuterRef("resource_type"), OuterRef("resource__object_id"), OuterRef("relation"), using
        ),
        expires_at=F("expires_at"),
        condition=F("condition"),
        condition_key=F("condition_key"),
        pass_id=Value(pass_id),
        round=Value(0),
    )
    changed = _members(
        projected, using=using, stats=stats, pass_id=pass_id, round_=0, region=region
    )
    types = region_types(using, region)
    for definition in program.baseline.definitions:
        if types is not None and definition.resource_type not in types:
            continue
        for relation in definition.relations:
            key = definition.resource_type, relation.name
            if key not in program.userset_relations:
                continue
            if not isinstance(relation.backing, ConstBinding) or relation.backing.filters:
                continue
            allowed = relation.allowed_subjects[0]
            triple = (allowed.type, relation.backing.target_id, allowed.relation)
            subject = intern([triple], using=using)[triple]
            sets = (
                IndexTerm.objects.using(using)
                .filter(type=key[0], relation=key[1])
                .exclude(object_id="*")
            )
            projected = select(
                sets,
                member_id=Value(subject),
                member_type=text(allowed.type),
                set_id=F("pk"),
                expires_at=Value(index_time.time_max()),
                condition=formula_value(None),
                condition_key=text(""),
                pass_id=Value(pass_id),
                round=Value(0),
            )
            changed += _members(
                projected, using=using, stats=stats, pass_id=pass_id, round_=0, region=region
            )
    return changed


def _membership_limits(*, using: str, region: int | None, stats: Stats) -> None:
    rows = restrict(IndexMember.objects.using(using), "set_id", using=using, region=region)
    groups = rows.exclude(condition_key="").order_by().values("set_id", "member_id").distinct()
    for group in groups.iterator(chunk_size=BATCH_SIZE):
        formulas = list(rows.filter(**group).values_list("condition", flat=True))
        stats.python_rows += len(formulas)
        conditions.enforce_limit(
            formulas, label=f"membership set={group['set_id']} member={group['member_id']}"
        )


def derive_memberships(program: IndexProgram, *, using: str, region: int | None = None) -> Stats:
    started = perf_counter()
    stats = Stats()
    logger.info("phase=memberships status=start region=%s", region)
    with _pass(using) as pass_id:
        changed = _membership_seed(
            program, using=using, region=region, stats=stats, pass_id=pass_id
        )
        round_ = 0
        while changed:
            round_started = perf_counter()
            outer = IndexMember.objects.using(using).alias(
                _inner=FilteredRelation("member__members")
            )
            if round_:
                outer = outer.filter(
                    Q(pass_id=pass_id, round=round_)
                    | Q(_inner__pass_id=pass_id, _inner__round=round_)
                )
            outer = outer.filter(_inner__isnull=False)
            columns = dict(
                member_id=F("_inner__member_id"),
                member_type=F("_inner__member_type"),
                set_id=F("set_id"),
                expires_at=Least("expires_at", "_inner__expires_at"),
                pass_id=Value(pass_id),
                round=Value(round_ + 1),
            )
            plain = outer.filter(condition_key="", _inner__condition_key="")
            next_changed = _members(
                select(plain, **columns, condition=formula_value(None), condition_key=text("")),
                using=using,
                stats=stats,
                pass_id=pass_id,
                round_=round_ + 1,
                region=region,
            )
            conditional = outer.filter(Q(condition_key__gt="") | Q(_inner__condition_key__gt=""))
            pairs = conditional.order_by().values("condition", "_inner__condition").distinct()
            for pair in pairs.iterator(chunk_size=BATCH_SIZE):
                stats.python_rows += 1
                formula = conditions.and_(pair["condition"], pair["_inner__condition"])
                if formula is False:
                    continue
                next_changed += _members(
                    select(
                        conditional.filter(formula_filter(pair)),
                        **columns,
                        condition=formula_value(formula),
                        condition_key=text(conditions.key(formula)),
                    ),
                    using=using,
                    stats=stats,
                    pass_id=pass_id,
                    round_=round_ + 1,
                    region=region,
                )
            logger.debug(
                "phase=memberships round=%s rows_out=%s python_rows=%s seconds=%.3f",
                round_,
                next_changed,
                stats.python_rows,
                perf_counter() - round_started,
            )
            changed = next_changed
            round_ += 1
        if program.conditional_nodes & program.userset_relations:
            _membership_limits(using=using, region=region, stats=stats)
    stats.seconds = perf_counter() - started
    logger.info(
        "phase=memberships status=done rows_out=%s python_rows=%s seconds=%.3f",
        stats.inserted + stats.updated,
        stats.python_rows,
        stats.seconds,
    )
    return stats


def _covers(
    source: QuerySet[Any],
    *,
    using: str,
    region: int | None,
    stats: Stats,
    pass_id: int,
    round_: int,
) -> int:
    rows = restrict(source, "scope_id", using=using, region=region)
    return upsert(
        rows, IndexCover, COVER_KEY, using=using, stats=stats, pass_id=pass_id, round_=round_
    )


def _columns(node: NodeSpec, pass_id: int, round_: int) -> dict[str, Any]:
    return dict(
        resource_type=text(node.type),
        node=text(node.name),
        pass_id=Value(pass_id),
        round=Value(round_),
    )


def _seed_relations(
    program: IndexProgram,
    *,
    using: str,
    region: int | None,
    stats: Stats,
    types: frozenset[str] | None,
    keys: frozenset[Key] | None = None,
) -> int:
    selected = Q(pk__in=[])
    for key, node in sorted(program.nodes.items()):
        if types is not None and key[0] not in types:
            continue
        if keys is not None and key not in keys:
            continue
        if node.kind == "relation" and key not in program.userset_only_relations:
            selected |= Q(resource_type=key[0], relation=key[1])
    edges = IndexEdge.objects.using(using).filter(selected)
    return _covers(
        select(
            edges,
            resource_type=F("resource_type"),
            node=F("relation"),
            pass_id=Value(0),
            round=Value(0),
            scope_id=F("resource_id"),
            holder_id=F("subject_id"),
            site=text(""),
            expires_at=F("expires_at"),
            condition=F("condition"),
            condition_key=F("condition_key"),
        ),
        using=using,
        region=region,
        stats=stats,
        pass_id=0,
        round_=0,
    )


def _site_arm(
    expr: PermRef,
    node: NodeSpec,
    program: IndexProgram,
    *,
    using: str,
    region: int | None,
    stats: Stats,
    pass_id: int,
    round_: int,
) -> int:
    site = program.nodes.get((node.type, expr.name))
    assert site is not None and site.operands is not None
    left = IndexCover.objects.using(using).filter(resource_type=node.type, node=site.operands[0])
    scopes = left.order_by().values("scope_id").annotate(_expires=Max("expires_at"))
    return _covers(
        select(
            scopes,
            **_columns(node, pass_id, round_),
            scope_id=F("scope_id"),
            holder_id=F("scope_id"),
            site=text(expr.name),
            expires_at=F("_expires"),
            condition=formula_value(None),
            condition_key=text(""),
        ),
        using=using,
        region=region,
        stats=stats,
        pass_id=pass_id,
        round_=round_,
    )


def _reference(
    expr: PermRef,
    node: NodeSpec,
    program: IndexProgram,
    *,
    using: str,
    region: int | None,
    stats: Stats,
    pass_id: int,
    round_: int,
    recursive: frozenset[Key],
) -> int:
    if expr.name in {"anonymous", "authenticated"}:
        if round_:
            return 0
        holder = AUTHENTICATED if expr.name == "authenticated" else anonymous()
        ids = intern([holder, type_level(node.type)], using=using)
        scope = ids[type_level(node.type)]
        source = IndexTerm.objects.using(using).filter(pk=scope)
        return _covers(
            select(
                source,
                **_columns(node, pass_id, round_),
                scope_id=F("pk"),
                holder_id=Value(ids[holder]),
                site=text(""),
                expires_at=Value(node.deadline or index_time.time_max()),
                condition=formula_value(None),
                condition_key=text(""),
            ),
            using=using,
            region=region,
            stats=stats,
            pass_id=pass_id,
            round_=round_,
        )
    target = program.nodes.get((node.type, expr.name))
    if target is not None and target.kind in {"and", "minus"}:
        return _site_arm(
            expr,
            node,
            program,
            using=using,
            region=region,
            stats=stats,
            pass_id=pass_id,
            round_=round_,
        )
    source = IndexCover.objects.using(using).filter(resource_type=node.type, node=expr.name)
    if round_:
        if (node.type, expr.name) not in recursive:
            return 0
        source = source.filter(pass_id=pass_id, round=round_ - 1)
    expiry = (
        Least("expires_at", Value(node.deadline)) if node.deadline is not None else F("expires_at")
    )
    return _covers(
        select(
            source,
            **_columns(node, pass_id, round_),
            scope_id=F("scope_id"),
            holder_id=F("holder_id"),
            site=F("site"),
            expires_at=expiry,
            condition=F("condition"),
            condition_key=F("condition_key"),
        ),
        using=using,
        region=region,
        stats=stats,
        pass_id=pass_id,
        round_=round_,
    )


def _arrow_lanes(
    source: QuerySet[Any],
    node: NodeSpec,
    *,
    using: str,
    region: int | None,
    stats: Stats,
    pass_id: int,
    round_: int,
    target_conditional: bool,
    edge_conditional: bool,
) -> int:
    columns = dict(
        **_columns(node, pass_id, round_),
        scope_id=F("_edge__resource_id"),
        holder_id=F("holder_id"),
        site=F("site"),
        expires_at=Least("expires_at", "_edge__expires_at"),
    )
    edge_q = Q(_edge__isnull=False)
    if region is not None:
        from .project import region_terms

        edge_q &= Q(_edge__resource_id__in=region_terms(using, region))
    plain = source.filter(edge_q & Q(condition_key="", _edge__condition_key=""))
    changed = _covers(
        select(plain, **columns, condition=formula_value(None), condition_key=text("")),
        using=using,
        region=region,
        stats=stats,
        pass_id=pass_id,
        round_=round_,
    )
    if target_conditional:
        one_row = source.filter(edge_q & Q(condition_key__gt="", _edge__condition_key=""))
        changed += _covers(
            select(one_row, **columns, condition=F("condition"), condition_key=F("condition_key")),
            using=using,
            region=region,
            stats=stats,
            pass_id=pass_id,
            round_=round_,
        )
    if edge_conditional:
        one_edge = source.filter(edge_q & Q(condition_key="", _edge__condition_key__gt=""))
        changed += _covers(
            select(
                one_edge,
                **columns,
                condition=F("_edge__condition"),
                condition_key=F("_edge__condition_key"),
            ),
            using=using,
            region=region,
            stats=stats,
            pass_id=pass_id,
            round_=round_,
        )
    if target_conditional and edge_conditional:
        both_q = edge_q & Q(condition_key__gt="", _edge__condition_key__gt="")
        pairs = source.filter(both_q).order_by().values("condition", "_edge__condition").distinct()
        for pair in pairs.iterator(chunk_size=BATCH_SIZE):
            stats.python_rows += 1
            formula = conditions.and_(pair["condition"], pair["_edge__condition"])
            if formula is False:
                continue
            changed += _covers(
                select(
                    source.filter(both_q & formula_filter(pair)),
                    **columns,
                    condition=formula_value(formula),
                    condition_key=text(conditions.key(formula)),
                ),
                using=using,
                region=region,
                stats=stats,
                pass_id=pass_id,
                round_=round_,
            )
    return changed


def _arrow(
    expr: PermArrow,
    node: NodeSpec,
    program: IndexProgram,
    *,
    using: str,
    region: int | None,
    stats: Stats,
    pass_id: int,
    round_: int,
    recursive: frozenset[Key],
) -> int:
    target_conditional = any(
        (type_, expr.target) in program.conditional_nodes
        for type_, name in program.nodes
        if name == expr.target
    )
    edge_conditional = (node.type, expr.via) in program.conditional_nodes
    source = (
        IndexCover.objects.using(using)
        .alias(
            _edge=FilteredRelation(
                "scope__edges_in",
                condition=Q(
                    scope__edges_in__resource_type=node.type, scope__edges_in__relation=expr.via
                ),
            )
        )
        .filter(node=expr.target)
    )
    if round_:
        source = source.filter(
            Q(resource_type__in=[t for t, n in recursive if n == expr.target])
            & Q(pass_id=pass_id, round=round_ - 1)
        )
    changed = _arrow_lanes(
        source,
        node,
        using=using,
        region=region,
        stats=stats,
        pass_id=pass_id,
        round_=round_,
        target_conditional=target_conditional,
        edge_conditional=edge_conditional,
    )
    # Only the types the arrow's relation allows as subjects can be its targets.
    subject_types = sorted(type_ for type_, name in node.deps if name == expr.target)
    if any((type_, expr.target) in program.type_level_nodes for type_ in subject_types):
        changed += _type_level_arrow(
            expr,
            node,
            subject_types=subject_types,
            edge_conditional=edge_conditional,
            using=using,
            region=region,
            stats=stats,
            pass_id=pass_id,
            round_=round_,
            recursive=recursive,
        )
    return changed


def _type_level_arrow(
    expr: PermArrow,
    node: NodeSpec,
    *,
    subject_types: list[str],
    edge_conditional: bool,
    using: str,
    region: int | None,
    stats: Stats,
    pass_id: int,
    round_: int,
    recursive: frozenset[Key],
) -> int:
    """A type-level row of the target applies at every edge whose target has its type.

    Type-level rows are few, one per distinct holder, so each is applied with
    one indexed query over the arrow's edges. A site held at the type-level
    scope is instantiated at the edge's target.
    """
    rows = IndexCover.objects.using(using).filter(
        node=expr.target, resource_type__in=subject_types, scope__relation="$type"
    )
    if round_:
        rows = rows.filter(
            resource_type__in=[t for t, n in recursive if n == expr.target],
            pass_id=pass_id,
            round=round_ - 1,
        )
    edges = (
        IndexEdge.objects.using(using)
        .filter(resource_type=node.type, relation=expr.via)
        .exclude(target__object_id="*")
    )
    if region is not None:
        from .project import region_terms

        edges = edges.filter(resource_id__in=region_terms(using, region))
    changed = 0
    for row in (
        rows.order_by()
        .values("resource_type", "scope_id", "holder_id", "site", "expires_at", "condition")
        .iterator(chunk_size=BATCH_SIZE)
    ):
        stats.python_rows += 1
        at_target = row["site"] and row["holder_id"] == row["scope_id"]
        columns = dict(
            **_columns(node, pass_id, round_),
            scope_id=F("resource_id"),
            holder_id=F("target_id") if at_target else Value(row["holder_id"]),
            site=text(row["site"]),
            expires_at=Least(Value(row["expires_at"], output_field=DateTimeField()), "expires_at"),
        )
        targets = edges.filter(target__type=row["resource_type"])
        formula = row["condition"]
        changed += _covers(
            select(
                targets.filter(condition_key=""),
                **columns,
                condition=formula_value(formula),
                condition_key=text(conditions.key(formula)),
            ),
            using=using,
            region=region,
            stats=stats,
            pass_id=pass_id,
            round_=round_,
        )
        if not edge_conditional:
            continue
        pairs = targets.exclude(condition_key="").order_by().values("condition").distinct()
        for pair in pairs.iterator(chunk_size=BATCH_SIZE):
            stats.python_rows += 1
            combined = conditions.and_(formula, pair["condition"])
            if combined is False:
                continue
            changed += _covers(
                select(
                    targets.filter(formula_filter(pair)),
                    **columns,
                    condition=formula_value(combined),
                    condition_key=text(conditions.key(combined)),
                ),
                using=using,
                region=region,
                stats=stats,
                pass_id=pass_id,
                round_=round_,
            )
    return changed


def _mono(expr: PermExpr, node: NodeSpec, program: IndexProgram, **kwargs: Any) -> int:
    if isinstance(expr, PermNil):
        return 0
    if isinstance(expr, PermRef):
        return _reference(expr, node, program, **kwargs)
    if isinstance(expr, PermArrow):
        return _arrow(expr, node, program, **kwargs)
    if isinstance(expr, PermBinOp) and expr.op == "+":
        return _mono(expr.left, node, program, **kwargs) + _mono(
            expr.right, node, program, **kwargs
        )
    raise ValueError("Set operations must be named sites")


def _grant_limits(*, using: str, node: NodeSpec, region: int | None, stats: Stats) -> None:
    rows = restrict(
        IndexCover.objects.using(using).filter(resource_type=node.type, node=node.name),
        "scope_id",
        using=using,
        region=region,
    )
    groups = (
        rows.exclude(condition_key="").order_by().values("scope_id", "holder_id", "site").distinct()
    )
    for group in groups.iterator(chunk_size=BATCH_SIZE):
        formulas = list(rows.filter(**group).values_list("condition", flat=True))
        stats.python_rows += len(formulas)
        conditions.enforce_limit(formulas, label=f"{node.type}#{node.name}")


def derive_nodes(
    program: IndexProgram,
    *,
    using: str,
    region: int | None = None,
    selected_stratum: int | None = None,
) -> Stats:
    started = perf_counter()
    stats = Stats()
    logger.info("phase=nodes status=start region=%s", region)
    # A row is derived at a scope of its own type.
    types = region_types(using, region)
    if selected_stratum is None:
        _seed_relations(program, using=using, region=region, stats=stats, types=types)
    else:
        relation_keys = frozenset(program.strata[selected_stratum])
        _seed_relations(
            program,
            using=using,
            region=region,
            stats=stats,
            types=types,
            keys=relation_keys,
        )
    for stratum_number, whole in enumerate(program.strata):
        if selected_stratum is not None and stratum_number != selected_stratum:
            continue
        recursive = frozenset(whole)
        stratum = tuple(key for key in whole if types is None or key[0] in types)
        if not stratum:
            continue
        stratum_started = perf_counter()
        stratum_out = 0
        needs_delta = any(program.nodes[key].recursive for key in stratum)
        with _pass(using) if needs_delta else nullcontext(0) as pass_id:
            round_ = 0
            while True:
                changed = 0
                for key in stratum:
                    node = program.nodes[key]
                    rule_started = perf_counter()
                    before_python = stats.python_rows
                    if node.kind == "relation":
                        produced = 0
                    elif node.kind in {"and", "minus"}:
                        produced = 0
                    else:
                        assert node.expr is not None
                        produced = _mono(
                            node.expr,
                            node,
                            program,
                            using=using,
                            region=region,
                            stats=stats,
                            pass_id=pass_id,
                            round_=round_,
                            recursive=recursive,
                        )
                    changed += produced
                    stratum_out += produced
                    logger.debug(
                        "phase=nodes stratum=%s node=%s#%s rule=%s round=%s "
                        "rows_in=%s rows_out=%s python_rows=%s seconds=%.3f",
                        stratum_number,
                        node.type,
                        node.name,
                        node.kind,
                        round_,
                        0,
                        produced,
                        stats.python_rows - before_python,
                        perf_counter() - rule_started,
                    )
                if not changed or not needs_delta:
                    break
                round_ += 1
            for key in stratum:
                node = program.nodes[key]
                if node.kind == "mono" and key in program.conditional_nodes:
                    _grant_limits(using=using, node=node, region=region, stats=stats)
        logger.info(
            "phase=nodes status=stratum_done stratum=%s nodes=%s rows_out=%s "
            "python_rows=%s seconds=%.3f",
            stratum_number,
            stratum,
            stratum_out,
            stats.python_rows,
            perf_counter() - stratum_started,
        )
    stats.seconds = perf_counter() - started
    logger.info(
        "phase=nodes status=done rows_out=%s python_rows=%s seconds=%.3f",
        stats.inserted + stats.updated,
        stats.python_rows,
        stats.seconds,
    )
    return stats
