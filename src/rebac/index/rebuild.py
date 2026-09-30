"""Locked rebuilds and rollback-only, payload-complete drift detection."""

from __future__ import annotations

import heapq
import logging
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from tempfile import TemporaryFile
from time import monotonic
from typing import TYPE_CHECKING, TextIO, cast

from django.db import transaction
from django.db.models import Q, Subquery

from rebac.index.maintain import IndexMaintenance, dependent_types
from rebac.schema.serialization import canonical_json

logger = logging.getLogger("rebac.index")

if TYPE_CHECKING:
    from rebac.index.project import Stats


def _rebuild_locked(maintenance: IndexMaintenance, *, types: Sequence[str] | None = None) -> Stats:
    from rebac.index.derive import derive_memberships, derive_nodes
    from rebac.index.project import Stats, project_edges
    from rebac.index.terms import type_level
    from rebac.models.generation import SchemaGeneration
    from rebac.models.index import IndexCover, IndexEdge, IndexMember, IndexTerm

    started = monotonic()
    using = maintenance.using
    program = maintenance.load_program()
    original_pair = SchemaGeneration.objects.revision_pair(using)
    manual = maintenance.backend is not None and maintenance.backend._schema_is_manual
    expected_revision = program.revision if manual else original_pair[0] if original_pair else None
    publish = (
        types is None
        or maintenance.schema_changed
        or (original_pair is not None and original_pair[1] == expected_revision)
    )
    selected: set[str] | None = None
    region = None
    if types is not None:
        selected = dependent_types(program, types)
        maintenance.add_triples((type_level(type_) for type_ in sorted(selected)), phase="region")
        maintenance.add_terms(
            IndexTerm.objects.using(using).filter(type__in=selected), phase="region"
        )
        # Type markers ask the projector to include newly introduced identities
        # in these definitions, even if no corresponding IndexTerm exists yet.
        for type_ in sorted(selected):
            maintenance.work().create(
                pass_id=maintenance.pass_id,
                kind="type",
                term=IndexTerm.objects.using(using).get(
                    type=type_, object_id="*", relation="$type"
                ),
                phase="region",
            )
        region = maintenance.pass_id
        _seed_sources(maintenance, selected)
        maintenance.expand_region()
    scopes = maintenance.work(phase="region").values("term_id")
    covers = IndexCover.objects.using(using).all()
    memberships = IndexMember.objects.using(using).all()
    edges = IndexEdge.objects.using(using).all()
    if selected is not None:
        covers = covers.filter(scope_id__in=Subquery(scopes))
        memberships = memberships.filter(set_id__in=Subquery(scopes))
        edges = edges.filter(resource_type__in=selected)
    stats = Stats()
    phase_started = monotonic()
    stats.deleted += edges.delete()[0]
    logger.info(
        "phase=clear_edges rows_out=%s python_rows=0 seconds=%.3f",
        stats.deleted,
        monotonic() - phase_started,
    )
    for step in (project_edges, derive_memberships, derive_nodes):
        phase_started = monotonic()
        result = step(program, using=using, region=region)
        stats.add(result)
        logger.info(
            "phase=%s rows_out=%s python_rows=%s seconds=%.3f",
            step.__name__,
            result.inserted + result.updated,
            result.python_rows,
            monotonic() - phase_started,
        )
        if step is project_edges and selected is not None:
            maintenance.add_terms(
                IndexTerm.objects.using(using).filter(type__in=selected), phase="region"
            )
            maintenance.expand_region()
        if step is project_edges:
            # Projection can discover previously uninterned usersets and their
            # consumers. Delete derived rows only after that final expansion;
            # the lazy scope subquery then includes every scope we rederive.
            stats.deleted += covers.delete()[0]
            stats.deleted += memberships.delete()[0]
    SchemaGeneration.objects.using(using).bulk_create(
        [SchemaGeneration(pk=1, revision=program.revision)], ignore_conflicts=True
    )
    if publish:
        if manual:
            SchemaGeneration.objects.using(using).filter(pk=1).update(
                index_revision=program.revision, index_program=program.digest
            )
        else:
            SchemaGeneration.objects.publish_index(using, program=program.digest)
    stats.seconds = monotonic() - started
    return stats


def _vacuum_terms(*, using: str, defined: Iterable[str] = ()) -> None:
    """Vacuum after clearing this pass's work, while its global lock is held.

    The term of an object of a ``defined`` type stays: enumeration lists an
    object that holds a permission only through a type-level grant, with no
    row of its own.
    """
    from rebac.models.index import (
        IndexCover,
        IndexEdge,
        IndexMember,
        IndexTerm,
        IndexWork,
    )

    references = []
    for model, names in (
        (IndexEdge, ("resource_id", "subject_id", "target_id")),
        (IndexMember, ("member_id", "set_id")),
        (IndexCover, ("scope_id", "holder_id")),
    ):
        for name in names:
            references.append(model.objects.using(using).order_by().values_list(name, flat=True))
    references.append(
        IndexWork.objects.using(using)
        .filter(term_id__isnull=False)
        .order_by()
        .values_list("term_id", flat=True)
    )
    objects = Q(type__in=sorted(defined), relation="") & ~Q(object_id="*")
    IndexTerm.objects.using(using).exclude(objects).exclude(
        pk__in=references[0].union(*references[1:])
    ).delete()


def _seed_sources(maintenance: IndexMaintenance, selected: set[str]) -> None:
    """Include source identities introduced since the last projection."""
    from django.db.models import F, Value

    from rebac._id import resource_id_attr
    from rebac.field_backing import resolve_attribute_backing
    from rebac.index.terms import intern_from
    from rebac.models import active_relationship_model
    from rebac.models.index import IndexTerm
    from rebac.models.relationship import RelationshipQuerySet, RelationshipRegistryQuerySet
    from rebac.resources import model_for_resource_type, stores_rows

    using = maintenance.using
    tuples = cast(
        RelationshipQuerySet | RelationshipRegistryQuerySet,
        active_relationship_model().objects.using(using).filter(resource_type__in=selected),
    ).index_projection()
    maintenance.python_rows += intern_from(
        tuples.order_by()
        .values(type=F("resource_type"), object_id=F("resource_id"))
        .annotate(relation=Value(""))
        .values("type", "object_id", "relation"),
        using=using,
    )
    for definition in maintenance.load_program().baseline.definitions:
        if definition.resource_type not in selected:
            continue
        model = model_for_resource_type(definition.resource_type)
        if model is not None and stores_rows(model):
            maintenance.capture_values(
                model._base_manager.using(using).all(),
                definition.resource_type,
                resource_id_attr(model),
                phase="region",
            )
        for relation in definition.relations:
            attr = resolve_attribute_backing(definition, relation)
            if attr is not None:
                if attr.resource is None:
                    maintenance.capture_values(
                        attr.target_model._base_manager.using(using).all(),
                        definition.resource_type,
                        attr.field.attname,
                        phase="region",
                    )
                else:
                    maintenance.add_triples(
                        ((definition.resource_type, attr.resource, ""),), phase="region"
                    )
    maintenance.add_terms(IndexTerm.objects.using(using).filter(type__in=selected), phase="region")


def rebuild(*, using: str, types: Sequence[str] | None = None) -> Stats:
    """Rebuild all definitions, or selected definitions and their dependents."""
    started = monotonic()
    logger.info("phase=rebuild status=start using=%s types=%s", using, types)
    owner = IndexMaintenance(using=using, independent=True)
    with owner as maintenance:
        acquired = monotonic()
        logger.info("phase=rebuild status=locked lock_wait_seconds=%.3f", acquired - started)
        table_write_started = monotonic()
        result = _rebuild_locked(maintenance, types=types)
        # Already derived. Do not cause a second pass on context-manager exit.
        maintenance.completed_stats = result
    logger.info(
        "phase=rebuild status=committed lock_held_seconds=%.3f "
        "table_write_upper_bound_seconds=%.3f "
        "rows_out=%s python_rows=%s seconds=%.3f",
        monotonic() - acquired,
        monotonic() - table_write_started,
        result.inserted + result.updated,
        result.python_rows,
        monotonic() - started,
    )
    return result


def _identity(prefix: str) -> tuple[str, ...]:
    return tuple(prefix + "__" + field for field in ("type", "object_id", "relation"))


def _semantic_rows(using: str, types: set[str] | None) -> Iterator[str]:
    from rebac.models.index import IndexCover, IndexEdge, IndexMember

    payload = ("expires_at", "condition", "condition_key")
    cover_fields = (
        *_identity("scope"),
        "resource_type",
        "node",
        *_identity("holder"),
        "site",
        *payload,
    )
    tables = (
        (
            IndexEdge,
            "edge",
            (
                *_identity("resource"),
                "resource_type",
                "relation",
                *_identity("subject"),
                *_identity("target"),
                "source",
                *payload,
            ),
            "resource_type",
        ),
        (
            IndexMember,
            "member",
            (*_identity("member"), "member_type", *_identity("set"), *payload),
            "set__type",
        ),
        (IndexCover, "cover", cover_fields, "resource_type"),
    )
    for model, label, columns, type_field in tables:
        rows = model.objects.using(using).order_by()
        if types is not None:
            rows = rows.filter(**{type_field + "__in": types})
        for row in rows.values_list(*columns).iterator(chunk_size=1000):
            yield canonical_json((label, *row))


@contextmanager
def _sorted_rows(rows: Iterator[str]) -> Iterator[TextIO]:
    """External merge sort: bounded rows and bounded open files, preserving duplicates."""
    runs: list[TextIO] = []
    try:
        chunk: list[str] = []
        for row in rows:
            chunk.append(row + "\n")
            if len(chunk) == 1000:
                run = TemporaryFile(mode="w+t", encoding="utf-8")
                run.writelines(sorted(chunk))
                run.seek(0)
                runs.append(run)
                chunk.clear()
                if len(runs) == 32:
                    merged = TemporaryFile(mode="w+t", encoding="utf-8")
                    merged.writelines(heapq.merge(*runs))
                    merged.seek(0)
                    for old in runs:
                        old.close()
                    runs = [merged]
        if chunk or not runs:
            run = TemporaryFile(mode="w+t", encoding="utf-8")
            run.writelines(sorted(chunk))
            run.seek(0)
            runs.append(run)
        with TemporaryFile(mode="w+t", encoding="utf-8") as result:
            result.writelines(heapq.merge(*runs))
            result.seek(0)
            yield result
    finally:
        for pending_run in runs:
            pending_run.close()


def verify(*, using: str, types: Sequence[str] | None = None) -> list[str]:
    """Compare complete semantic rows under the maintenance lock; always roll back."""
    differences: list[str] = []
    with transaction.atomic(using=using):
        owner = IndexMaintenance(using=using, independent=True)
        with owner as maintenance:
            selected = None if types is None else set(types)
            if selected is not None:
                program = maintenance.load_program()
                selected = dependent_types(program, selected)
            with _sorted_rows(_semantic_rows(using, selected)) as live:
                maintenance.completed_stats = _rebuild_locked(maintenance, types=types)
                with _sorted_rows(_semantic_rows(using, selected)) as derived:
                    old, new = next(live, None), next(derived, None)
                    while old is not None or new is not None:
                        if old == new:
                            old, new = next(live, None), next(derived, None)
                        elif old is not None and (new is None or old < new):
                            differences.append("unexpected " + old.rstrip("\n"))
                            old = next(live, None)
                        else:
                            assert new is not None
                            differences.append("missing " + new.rstrip("\n"))
                            new = next(derived, None)
        transaction.set_rollback(True, using=using)
    return sorted(differences)
