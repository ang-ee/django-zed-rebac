"""Project stored and live-backed relationships with ordinary Django querysets."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import batched
from time import perf_counter
from typing import Any, cast

from django.db import connections
from django.db.models import (
    BigIntegerField,
    CharField,
    Exists,
    F,
    JSONField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce

from rebac.errors import SchemaError
from rebac.field_backing import (
    _resolve_attribute_backing,
    _resolve_field_backing,
    const_backing_model_errors,
    resolve_const_backing,
)
from rebac.models import active_relationship_model
from rebac.models.index import IndexCover, IndexEdge, IndexTerm, IndexWork
from rebac.models.relationship import RelationshipQuerySet, RelationshipRegistryQuerySet
from rebac.schema.ast import AttributeBinding, ConstBinding, Definition, FieldBinding, Relation

from . import conditions
from . import time as index_time
from .codec import identity_codec
from .program import IndexProgram
from .terms import AUTHENTICATED, anonymous, intern, intern_from, type_level
from .write import projected_rows, stream_create

BATCH_SIZE = 256
logger = logging.getLogger("rebac.index")


@dataclass
class Stats:
    inserted: int = 0
    deleted: int = 0
    updated: int = 0
    python_rows: int = 0
    seconds: float = 0.0
    changed_scopes: set[int] = field(default_factory=set)
    changed_nodes: dict[tuple[str, str], set[int]] = field(default_factory=dict)
    added_edges: set[tuple[int, str]] = field(default_factory=set)
    removed_edges: set[tuple[int, str]] = field(default_factory=set)
    edge_candidates: dict[tuple[Any, ...], dict[str, Any]] | None = None

    def add(self, other: Stats) -> None:
        self.inserted += other.inserted
        self.deleted += other.deleted
        self.updated += other.updated
        self.python_rows += other.python_rows
        self.seconds += other.seconds
        self.changed_scopes.update(other.changed_scopes)
        for key, scopes in other.changed_nodes.items():
            self.changed_nodes.setdefault(key, set()).update(scopes)
        self.added_edges.update(other.added_edges)
        self.removed_edges.update(other.removed_edges)


def text(value: str) -> Value:
    return Value(value, output_field=CharField())


def formula_value(value: conditions.Formula) -> Value:
    return Value(value, output_field=JSONField())


def formula_filter(values: dict[str, Any]) -> Q:
    """JSONField exact=None means JSON null; unconditional formulas use SQL NULL."""
    result = Q()
    for name, value in values.items():
        result &= Q(**{name + "__isnull": True}) if value is None else Q(**{name: value})
    return result


def select(queryset: QuerySet[Any], **columns: Any) -> QuerySet[Any]:
    """Rename a projection without annotation clashes with model field names."""
    aliases = {f"_index_col_{i}": expression for i, expression in enumerate(columns.values())}
    queryset = queryset.order_by().annotate(**aliases).values(*aliases)
    return cast(
        QuerySet[Any],
        queryset.annotate(
            **{name: F(alias) for name, alias in zip(columns, aliases, strict=True)}
        ).values(*columns),
    )


def region_terms(using: str, region: int) -> QuerySet[Any]:
    return cast(
        QuerySet[Any],
        IndexWork.objects.using(using).filter(pass_id=region, phase="region").values("term_id"),
    )


def region_types(using: str, region: int | None) -> frozenset[str] | None:
    """The resource types a pass touches; ``None`` for a rebuild of everything.

    A row is derived at a scope of its own type, so a pass has nothing to do
    for a definition whose type is not in its region.
    """
    if region is None:
        return None
    terms = IndexTerm.objects.using(using).filter(pk__in=region_terms(using, region))
    return frozenset(terms.order_by().values_list("type", flat=True).distinct())


def _source_region(resource_type: str, rid: Any, relation: Any, *, using: str, region: int) -> Q:
    allowed = IndexTerm.objects.using(using).filter(pk__in=region_terms(using, region))
    whole_type = IndexWork.objects.using(using).filter(
        pass_id=region,
        phase="region",
        kind="type",
        term__type=resource_type,
    )
    return Q(Exists(allowed.filter(type=resource_type, object_id=rid, relation=relation))) | Q(
        Exists(whole_type)
    )


def restrict(source: QuerySet[Any], field: str, *, using: str, region: int | None) -> QuerySet[Any]:
    if region is None:
        return source
    return source.filter(**{f"{field}__in": region_terms(using, region)})


def upsert(
    source: QuerySet[Any] | Iterable[tuple[Any, ...]],
    model: Any,
    keys: tuple[str, ...],
    *,
    using: str,
    stats: Stats,
    pass_id: int | None = None,
    round_: int | None = None,
) -> int:
    """Keep maximum expiry per identity under the maintenance owner's lock."""
    fields = tuple(field.attname for field in model._meta.concrete_fields if not field.primary_key)
    changed = 0
    batch_size = min(
        BATCH_SIZE, (connections[using].features.max_query_params or 65535) // len(keys)
    )
    rows = projected_rows(source, fields, using=using) if isinstance(source, QuerySet) else source
    for batch in batched(rows, batch_size, strict=False):
        stats.python_rows += len(batch)
        candidates: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in batch:
            values = dict(zip(fields, row, strict=True))
            key = tuple(values[name] for name in keys)
            previous = candidates.get(key)
            if previous is None or values["expires_at"] > previous["expires_at"]:
                candidates[key] = values
        if (model is IndexEdge or (model is IndexCover and pass_id == 0)) and all(
            values["expires_at"] == index_time.time_max() for values in candidates.values()
        ):
            for values in candidates.values():
                if pass_id is not None:
                    values.update(pass_id=pass_id, round=round_)
            update_fields = ["expires_at"]
            if model is IndexCover:
                update_fields.extend(("pass_id", "round"))
            submitted = stream_create(
                (tuple(values[field] for field in fields) for values in candidates.values()),
                model,
                fields,
                using=using,
                update_conflicts=True,
                update_fields=update_fields,
                unique_fields=keys
                if connections[using].features.supports_update_conflicts_with_target
                else None,
            )
            stats.inserted += submitted
            changed += submitted
            continue
        lookup = Q()
        for key in candidates:
            lookup |= Q(**dict(zip(keys, key, strict=True)))
        existing = {
            tuple(row[:-1]): row[-1]
            for row in model.objects.using(using).filter(lookup).values_list(*keys, "expires_at")
        }
        stats.python_rows += len(existing)
        kept = []
        for key, values in candidates.items():
            expiry = existing.get(key)
            if expiry is not None and expiry >= values["expires_at"]:
                continue
            if pass_id is not None:
                values.update(pass_id=pass_id, round=round_)
            kept.append(tuple(values[field] for field in fields))
            if expiry is None:
                stats.inserted += 1
            else:
                stats.updated += 1
        update_fields = ["expires_at"]
        if model is not IndexEdge:
            update_fields.extend(("pass_id", "round"))
        changed += stream_create(
            kept,
            model,
            fields,
            using=using,
            update_conflicts=True,
            update_fields=update_fields,
            unique_fields=keys
            if connections[using].features.supports_update_conflicts_with_target
            else None,
        )
    return changed


def _term(source: QuerySet[Any], type_: Any, object_id: Any, relation: Any, using: str) -> int:
    return intern_from(
        select(source, type=type_, object_id=object_id, relation=relation), using=using
    )


def _term_id(type_: Any, object_id: Any, relation: Any, using: str) -> Subquery:
    return Subquery(
        IndexTerm.objects.using(using)
        .filter(type=type_, object_id=object_id, relation=relation)
        .values("pk")[:1],
        output_field=BigIntegerField(),
    )


def _emit(
    rows: QuerySet[Any],
    *,
    resource_type: str,
    relation: str,
    source: str,
    condition: conditions.Formula,
    using: str,
    region: int | None,
    stats: Stats,
) -> None:
    """Input columns: rid, rr, st, sid, sr, expiry (all canonical wire values)."""
    if condition is False:
        return
    if region is not None:
        rows = rows.filter(
            _source_region(
                resource_type, OuterRef("rid"), OuterRef("rr"), using=using, region=region
            )
        )
    identities = select(rows, type=text(resource_type), object_id=F("rid"), relation=F("rr")).union(
        select(rows, type=F("st"), object_id=F("sid"), relation=F("sr")),
        select(rows, type=F("st"), object_id=F("sid"), relation=text("")),
        select(rows, type=text(resource_type), object_id=F("rid"), relation=text(relation)),
    )
    stats.python_rows += intern_from(identities, using=using, distinct=False)
    if region is not None:
        # New source/set identities must be visible to the subsequent DRed pass.
        for suffix, kind in ((OuterRef("rr"), "scope"), (relation, "set")):
            work = select(
                rows,
                pass_id=Value(region),
                kind=text(kind),
                term_id=_term_id(resource_type, OuterRef("rid"), suffix, using),
                node=text(""),
                phase=text("region"),
            ).distinct()
            existing = IndexWork.objects.using(using).filter(
                pass_id=region, phase="region", term_id=OuterRef("term_id")
            )
            stats.python_rows += stream_create(
                work.filter(~Exists(existing)),
                IndexWork,
                ("pass_id", "kind", "term_id", "node", "phase"),
                using=using,
            )
        # New userset targets may be constant-backed, with no source row of
        # their own. Their memberships and existing consumers still belong to
        # this pass. Ordinary actor targets do not widen the resource region.
        for suffix, kind in ((OuterRef("sr"), "set"), ("", "scope")):
            targets = select(
                rows.exclude(sr=""),
                pass_id=Value(region),
                kind=text(kind),
                term_id=_term_id(OuterRef("st"), OuterRef("sid"), suffix, using),
                node=text(""),
                phase=text("region"),
            ).distinct()
            existing = IndexWork.objects.using(using).filter(
                pass_id=region, phase="region", term_id=OuterRef("term_id")
            )
            stats.python_rows += stream_create(
                targets.filter(~Exists(existing)),
                IndexWork,
                ("pass_id", "kind", "term_id", "node", "phase"),
                using=using,
            )
    projected = select(
        rows,
        resource_id=_term_id(resource_type, OuterRef("rid"), OuterRef("rr"), using),
        resource_type=text(resource_type),
        relation=text(relation),
        subject_id=_term_id(OuterRef("st"), OuterRef("sid"), OuterRef("sr"), using),
        target_id=_term_id(OuterRef("st"), OuterRef("sid"), "", using),
        source=text(source),
        expires_at=F("expiry"),
        condition=formula_value(condition),
        condition_key=text(conditions.key(condition)),
    )
    if stats.edge_candidates is None:
        upsert(
            projected,
            IndexEdge,
            ("resource_id", "relation", "subject_id", "source", "condition_key"),
            using=using,
            stats=stats,
        )
    else:
        fields = (
            "resource_id",
            "resource_type",
            "relation",
            "subject_id",
            "target_id",
            "source",
            "expires_at",
            "condition",
            "condition_key",
        )
        for row in projected_rows(projected, fields, using=using):
            stats.python_rows += 1
            values = cast(dict[str, Any], dict(zip(fields, row, strict=True)))
            key = tuple(
                values[name]
                for name in ("resource_id", "relation", "subject_id", "source", "condition_key")
            )
            previous = stats.edge_candidates.get(key)
            if previous is None or values["expires_at"] > previous["expires_at"]:
                stats.edge_candidates[key] = values


def _write_edge_difference(stats: Stats, *, using: str, sources: set[int]) -> None:
    """Apply only changed projected edges in the bounded source region."""
    assert stats.edge_candidates is not None
    fields = (
        "resource_id",
        "resource_type",
        "relation",
        "subject_id",
        "target_id",
        "source",
        "expires_at",
        "condition",
        "condition_key",
    )
    key_fields = ("resource_id", "relation", "subject_id", "source", "condition_key")
    complete_sources = set(sources)
    # Projection may discover a new target scope partway through its ordered
    # rules. The rules that ran before it joined never visited it, so its
    # candidates are a subset of its true edges: they can add an edge that is
    # missing, but they neither remove nor replace one that is stored. (Two
    # source rows may share a key and keep the later expiry; a partial
    # candidate may hold only the earlier one.)
    sources.update(values["resource_id"] for values in stats.edge_candidates.values())
    max_params = connections[using].features.max_query_params or 5000
    batch_size = max(1, min(5000, max_params - 32))
    old = {
        tuple(getattr(edge, name) for name in key_fields): edge
        for source_batch in batched(
            sorted(source for source in sources if source is not None), batch_size, strict=False
        )
        for edge in IndexEdge.objects.using(using)
        .filter(resource_id__in=source_batch)
        .iterator(chunk_size=BATCH_SIZE)
    }
    stats.python_rows += len(old)

    def mark(type_: str, relation: str, scope: int) -> None:
        stats.changed_scopes.add(scope)
        stats.changed_nodes.setdefault((type_, relation), set()).add(scope)

    removed = {
        key for key in old if key[0] in complete_sources and key not in stats.edge_candidates
    }
    if removed:
        doomed = [old[key].pk for key in removed]
        for key in removed:
            mark(old[key].resource_type, old[key].relation, key[0])
            stats.removed_edges.add((key[0], old[key].relation))
        for batch in batched(doomed, batch_size, strict=False):
            stats.deleted += IndexEdge.objects.using(using).filter(pk__in=batch).delete()[0]
    created = []
    updated = []
    for key, values in stats.edge_candidates.items():
        previous = old.get(key)
        if previous is None:
            created.append(tuple(values[name] for name in fields))
            mark(values["resource_type"], values["relation"], values["resource_id"])
            stats.added_edges.add((values["resource_id"], values["relation"]))
        elif key[0] in complete_sources and any(
            getattr(previous, name) != values[name] for name in fields
        ):
            for name in fields:
                setattr(previous, name, values[name])
            updated.append(previous)
            mark(values["resource_type"], values["relation"], values["resource_id"])
            stats.added_edges.add((values["resource_id"], values["relation"]))
            stats.removed_edges.add((values["resource_id"], values["relation"]))
    stats.inserted += stream_create(created, IndexEdge, fields, using=using)
    if updated:
        IndexEdge.objects.using(using).bulk_update(
            updated,
            ("resource_type", "target_id", "expires_at", "condition"),
            batch_size=BATCH_SIZE,
        )
        stats.updated += len(updated)


def _stored(
    relation: Relation,
    resource_type: str,
    program: IndexProgram,
    *,
    using: str,
    region: int | None,
    stats: Stats,
) -> None:
    backing = relation.backing
    if backing is not None and not (
        isinstance(backing, AttributeBinding) and backing.resource is not None
    ):
        return
    rows = (
        cast(
            RelationshipQuerySet | RelationshipRegistryQuerySet,
            active_relationship_model().objects.using(using),
        )
        .index_projection()
        .filter(
            resource_type=resource_type,
            relation=relation.name,
        )
    )
    if region is not None:
        rows = rows.filter(
            _source_region(resource_type, OuterRef("resource_id"), "", using=using, region=region)
        )
    if isinstance(backing, AttributeBinding):
        rows = rows.exclude(resource_id=backing.resource)
    if not relation.with_expiration:
        rows = rows.filter(expires_at__isnull=True)
    valid = Q(pk__in=[])
    for allowed in relation.allowed_subjects:
        arm = Q(
            subject_type=allowed.type,
            subject_relation=allowed.relation,
            caveat_name=allowed.with_caveat,
        )
        if allowed.wildcard:
            arm &= Q(subject_id="*")
        else:
            arm &= ~Q(subject_id="*")
            if allowed.id:
                arm &= Q(subject_id=allowed.id)
        valid |= arm
    rows = rows.filter(valid)
    if not any(allowed.with_caveat for allowed in relation.allowed_subjects):
        _emit(
            select(
                rows,
                rid=F("resource_id"),
                rr=text(""),
                st=F("subject_type"),
                sid=F("subject_id"),
                sr=F("subject_relation"),
                expiry=Coalesce("expires_at", Value(index_time.TIME_MAX)),
            ),
            resource_type=resource_type,
            relation=relation.name,
            source="tuple",
            condition=None,
            using=using,
            region=region,
            stats=stats,
        )
        return
    # Each distinct caveat instance is a formula lane, not a relationship loop.
    instances = rows.order_by().values("caveat_name", "caveat_context").distinct()
    for instance in instances.iterator(chunk_size=BATCH_SIZE):
        stats.python_rows += 1
        condition = conditions.pinned(
            instance["caveat_name"],
            instance["caveat_context"],
            program.baseline,
            program.recaveated,
        )
        selected = rows.filter(formula_filter(instance))
        _emit(
            select(
                selected,
                rid=F("resource_id"),
                rr=text(""),
                st=F("subject_type"),
                sid=F("subject_id"),
                sr=F("subject_relation"),
                expiry=Coalesce("expires_at", Value(index_time.TIME_MAX)),
            ),
            resource_type=resource_type,
            relation=relation.name,
            source="tuple",
            condition=condition,
            using=using,
            region=region,
            stats=stats,
        )


def _backed(
    definition: Definition,
    relation: Relation,
    *,
    using: str,
    region: int | None,
    stats: Stats,
) -> None:
    backing = relation.backing
    if backing is None:
        return
    allowed = relation.allowed_subjects[0]
    rr = ""
    label = f"{definition.resource_type}#{relation.name}"
    if isinstance(backing, FieldBinding):
        # The raising resolver keeps the reason, which rebac.E009 also reports.
        try:
            resolved = _resolve_field_backing(definition, relation)
        except ValueError as exc:
            raise SchemaError(f"Cannot resolve {label}: {exc} (rebac.E009)") from exc
        rows = resolved.queryset(using=using)
        rid = identity_codec(resolved.source_model, resolved.source_id_attr).to_wire(
            resolved.source_values_path()
        )
        sid = identity_codec(resolved.target_model, resolved.target_id_attr).to_wire(
            resolved.target_values_path()
        )
        source = "field"
    elif isinstance(backing, AttributeBinding):
        try:
            attribute = _resolve_attribute_backing(definition, relation)
        except ValueError as exc:
            raise SchemaError(f"Cannot resolve {label}: {exc} (rebac.E009)") from exc
        rows = attribute.target_model._base_manager.using(using).filter(**attribute.filters)
        if attribute.resource is not None:
            rows = attribute.target_model._base_manager.using(using).filter(
                attribute.subjects_filter(attribute.resource)
            )
            rid = text(attribute.resource)
        else:
            rid = identity_codec(attribute.target_model, attribute.field.name).to_wire(
                attribute.field.name
            )
        sid = identity_codec(attribute.target_model, attribute.target_id_attr).to_wire(
            attribute.target_id_attr
        )
        source = "attribute"
    else:
        assert isinstance(backing, ConstBinding)
        if backing.filters:
            const = resolve_const_backing(definition, relation)
            if const is None:
                reasons = "; ".join(const_backing_model_errors(definition, relation))
                raise SchemaError(f"Cannot resolve {label}: {reasons or 'unresolved'} (rebac.E009)")
            rows = const.source_model._base_manager.using(using).filter(**const.filters)
            rid = identity_codec(const.source_model, const.source_id_attr).to_wire(
                const.source_values_path()
            )
        else:
            ids = intern([type_level(definition.resource_type)], using=using)
            stats.python_rows += len(ids)
            rows = IndexTerm.objects.using(using).filter(
                pk=ids[type_level(definition.resource_type)]
            )
            rid = text("*")
            rr = "$type"
        sid = text(backing.target_id)
        source = "const"
    projected = select(
        rows,
        rid=rid,
        rr=text(rr),
        st=text(allowed.type),
        sid=sid,
        sr=text(allowed.relation),
        expiry=Value(index_time.TIME_MAX),
    )
    projected = projected.filter(
        Q(rid__isnull=False) & Q(sid__isnull=False) & ~Q(rid="") & ~Q(sid="")
    )
    if not rr and projected.filter(rid="*").exists():
        raise SchemaError(
            f"A dynamic backing produced reserved resource ID '*': {definition.resource_type}"
        )
    _emit(
        projected,
        resource_type=definition.resource_type,
        relation=relation.name,
        source=source,
        condition=None,
        using=using,
        region=region,
        stats=stats,
    )


def project_edges(
    program: IndexProgram, *, using: str, region: int | None = None, diff: bool = False
) -> Stats:
    started = perf_counter()
    stats = Stats()
    source_scopes: set[int] = set()
    if diff:
        assert region is not None
        stats.edge_candidates = {}
        source_scopes = set(region_terms(using, region).values_list("term_id", flat=True))
    logger.info("phase=projection status=start region=%s", region)
    types = region_types(using, region)
    selected = [
        definition
        for definition in program.baseline.definitions
        if types is None or definition.resource_type in types
    ]
    if region is None:
        # The owners refuse these when a relationship is written. A rebuild
        # also reads rows that no owner wrote.
        invalid = (
            cast(
                RelationshipQuerySet | RelationshipRegistryQuerySet,
                active_relationship_model().objects.using(using),
            )
            .index_projection()
            .filter(
                Q(resource_id="*")
                | Q(resource_id="")
                | Q(expires_at__lte=index_time.TIME_MIN)
                | Q(expires_at__gte=index_time.TIME_MAX)
            )
        )
        if invalid.exists():
            raise SchemaError("Invalid resource ID or expiration in relationship projection")
    reserved = {AUTHENTICATED, anonymous()}
    definitions: dict[tuple[str, str], tuple[Definition, Relation]] = {}
    for definition in selected:
        for relation in definition.relations:
            definitions.setdefault(
                (definition.resource_type, relation.name), (definition, relation)
            )
        reserved.add(type_level(definition.resource_type))
        reserved.add((definition.resource_type, "*", ""))
    stats.python_rows += len(intern(sorted(reserved), using=using))
    from rebac._id import resource_id_attr
    from rebac.resources import model_for_resource_type, stores_rows

    # A pass interns the rows its owner wrote when it captures them; only a
    # rebuild reads the model tables.
    for definition in selected if region is None else ():
        model = model_for_resource_type(definition.resource_type)
        if model is None or not stores_rows(model):
            continue
        attr = resource_id_attr(model)
        codec = identity_codec(model, attr)
        model_rows = (
            model._base_manager.using(using)
            .order_by()
            .annotate(_index_identity=codec.to_wire(attr))
            .exclude(_index_identity__isnull=True)
            .exclude(_index_identity="")
        )
        stats.python_rows += intern_from(
            select(
                model_rows,
                type=text(definition.resource_type),
                object_id=F("_index_identity"),
                relation=text(""),
            ),
            using=using,
        )
    for (resource_type, _name), (definition, relation) in sorted(definitions.items()):
        rule_started = perf_counter()
        before_python = stats.python_rows
        before_out = stats.inserted + stats.updated
        _stored(relation, resource_type, program, using=using, region=region, stats=stats)
        _backed(definition, relation, using=using, region=region, stats=stats)
        logger.debug(
            "phase=projection node=%s#%s rows_in=%s rows_out=%s python_rows=%s seconds=%.3f",
            resource_type,
            relation.name,
            IndexEdge.objects.using(using)
            .filter(resource_type=resource_type, relation=relation.name)
            .count()
            if logger.isEnabledFor(logging.DEBUG)
            else 0,
            stats.inserted + stats.updated - before_out,
            stats.python_rows - before_python,
            perf_counter() - rule_started,
        )
    # Concrete usersets referenced by another relation need a set term even
    # when their own relation is supplied entirely by an unfiltered constant.
    for resource_type, relation_name in sorted(program.userset_relations):
        if types is not None and resource_type not in types:
            continue
        objects = (
            IndexTerm.objects.using(using)
            .filter(type=resource_type, relation="")
            .exclude(object_id__in=("*", ""))
        )
        if region is not None:
            selected = IndexTerm.objects.using(using).filter(pk__in=region_terms(using, region))
            objects = objects.filter(
                object_id__in=selected.filter(type=resource_type).values("object_id")
            )
        stats.python_rows += _term(objects, F("type"), F("object_id"), text(relation_name), using)
        if region is not None:
            # A newly referenced constant set has no edge of its own at this
            # object. Still put its membership and consumers into DRed.
            selected = IndexTerm.objects.using(using).filter(pk__in=region_terms(using, region))
            sets = IndexTerm.objects.using(using).filter(
                type=resource_type,
                relation=relation_name,
                object_id__in=selected.filter(type=resource_type).values("object_id"),
            )
            existing = IndexWork.objects.using(using).filter(
                pass_id=region, phase="region", term_id=OuterRef("pk")
            )
            stats.python_rows += stream_create(
                select(
                    sets.filter(~Exists(existing)),
                    pass_id=Value(region),
                    kind=text("set"),
                    term_id=F("pk"),
                    node=text(""),
                    phase=text("region"),
                ),
                IndexWork,
                ("pass_id", "kind", "term_id", "node", "phase"),
                using=using,
            )
    if diff:
        assert region is not None
        _write_edge_difference(stats, using=using, sources=source_scopes)
        stats.edge_candidates = None
    stats.seconds = perf_counter() - started
    logger.info(
        "phase=projection status=done rows_out=%s python_rows=%s seconds=%.3f",
        stats.inserted + stats.updated,
        stats.python_rows,
        stats.seconds,
    )
    return stats
