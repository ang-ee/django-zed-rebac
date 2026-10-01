"""Transaction owners and materialized old/new frontiers for the permission index."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar, Token
from itertools import batched
from time import monotonic
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

from django.db import DatabaseError, connections, models, transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery, Value

from rebac.errors import SchemaError
from rebac.schema.ast import PermArrow, PermBinOp, PermRef
from rebac.types import RelationshipFilter, RelationshipTuple

if TYPE_CHECKING:
    from rebac.backends.local import LocalBackend
    from rebac.field_backing import ModelField
    from rebac.index.program import IndexProgram, WatchSpec
    from rebac.index.project import Stats

logger = logging.getLogger("rebac.index")
_passes: ContextVar[tuple[IndexMaintenance, ...]] = ContextVar("rebac_index_passes", default=())
_deferred: ContextVar[tuple[tuple[str, int, Any], ...]] = ContextVar(
    "rebac_index_deferred", default=()
)


def _vacuum_model_terms(*, using: str) -> None:
    """PK snapshots are private work identities, never graph identities."""
    from rebac.models.index import IndexTerm, IndexWork

    IndexTerm.objects.using(using).filter(type__startswith="$model/").filter(
        ~Exists(IndexWork.objects.using(using).filter(term_id=OuterRef("pk")))
    ).delete()


def defer_signal_pass(maintenance: IndexMaintenance, caller_atomic: Any) -> None:
    """Retain a live collector's captures until its caller transaction ends."""
    using, pass_id = maintenance.using, maintenance.pass_id
    if caller_atomic is None:
        # No failure signal exists for an autocommit save. Its abandoned work
        # is reaped by the next locked owner (the matching post adopts first).
        return
    _deferred.set((*_deferred.get(), (using, pass_id, caller_atomic)))

    def cleanup() -> None:
        if not any(alias == using and pending == pass_id for alias, pending, _ in _deferred.get()):
            return
        with IndexMaintenance(using=using):
            pass

    transaction.on_commit(cleanup, using=using)


def forget_signal_pass(using: str, pass_id: int) -> None:
    _deferred.set(tuple(item for item in _deferred.get() if item[:2] != (using, pass_id)))


def current_pass(using: str) -> IndexMaintenance | None:
    return next((item for item in reversed(_passes.get()) if item.using == using), None)


def model_lineage(model: type[models.Model]) -> tuple[type[models.Model], ...]:
    """Include proxy targets and every concrete ancestor of an MTI write."""
    concrete = model._meta.concrete_model
    assert concrete is not None
    return tuple(
        dict.fromkeys(
            (model, concrete, *(parent for parent in concrete._meta.all_parents if parent))
        )
    )


def model_is_watched(
    watched: Mapping[str, WatchSpec], model: type[models.Model], names: Iterable[str] | None = None
) -> bool:
    specs = [
        watch
        for candidate in model_lineage(model)
        if (watch := watched.get(candidate._meta.label_lower)) is not None
        and watch.model is candidate
    ]
    if names is None:
        return bool(specs)
    normalized = set(names)
    for field in model._meta.concrete_fields:
        if field.name in normalized or field.attname in normalized:
            normalized.update((field.name, field.attname))
    return any(normalized & watch.fields for watch in specs)


def installed(using: str, backend: LocalBackend) -> bool:
    """Whether a policy is installed on the alias, so that there is an index to maintain.

    Before the first ``sync`` there is none: a write proceeds, the index stays
    unpublished and reads stay closed. While migrations run, the library's own
    tables can be missing or lack a column. The witness cannot be read then,
    and the answer is the same. The read has its own savepoint, so a failure
    leaves the caller's transaction usable.
    """
    from rebac.models.generation import SchemaGeneration

    if backend._schema_is_manual:
        return True
    try:
        with transaction.atomic(using=using):
            witness = SchemaGeneration.objects.witness(using)
    except DatabaseError:
        return False
    return witness is not None and bool(witness.revision)


def get_program(using: str, backend: LocalBackend | None = None) -> IndexProgram:
    from rebac.backends.local import LocalBackend
    from rebac.index.program import program_for
    from rebac.index.read import _backend

    active = backend if backend is not None else _backend()
    if not isinstance(active, LocalBackend):
        raise SchemaError("The permission index requires LocalBackend.")
    program = program_for(active, using=using)
    return program


def dependent_types(program: IndexProgram, types: Iterable[str]) -> set[str]:
    result = set(types)
    while True:
        before = len(result)
        seeds = [key for key in program.nodes if key[0] in result]
        result.update(key[0] for key in program.dependents(seeds))
        if len(result) == before:
            return result


class IndexMaintenance:
    """Serialize source mutation and DRed in one transaction, nesting by alias.

    Work rows have kind ``scope`` (object or type-level term) or ``set``
    (userset term). An empty node selects all nodes at that scope. Source
    identities and reverse-path results are inserted before mutation, never
    retained as lazy querysets for later evaluation.
    """

    def __init__(
        self,
        *,
        using: str,
        backend: LocalBackend | None = None,
        resume_pass: int | None = None,
        independent: bool = False,
    ) -> None:
        self.using = using
        self.backend = backend
        self.pass_id = 0
        self.program: IndexProgram | None = None
        self.schema_changed = False
        self.schema_types: set[str] = set()
        self.schema_all = False
        self.schema_initial = False
        self.python_rows = 0
        self.statements = 0
        self._outer: IndexMaintenance | None = None
        self._outer_state: tuple[bool, set[str], bool, bool, int] | None = None
        self._token: Token[tuple[IndexMaintenance, ...]] | None = None
        self._stack = ExitStack()
        self._started = 0.0
        self.deferred = False
        self._independent = independent
        self.completed_stats: Stats | None = None
        self.resume_pass = resume_pass
        self._region_ids: set[int] | None = None

    def __enter__(self) -> IndexMaintenance:
        from rebac.models.index import IndexState, IndexWork

        with ExitStack() as stack:
            connection = connections[self.using]
            stack.enter_context(
                transaction.atomic(
                    using=self.using,
                    savepoint=connection.in_atomic_block,
                )
            )
            self._outer = None if self._independent else current_pass(self.using)
            if self._outer is not None:
                if self.backend is not None and self._outer.backend is not self.backend:
                    if self._outer.backend is not None or self._outer.program is not None:
                        error = SchemaError("Nested index writes must use the same backend/schema.")
                        raise error
                    self._outer.backend = self.backend
                self._outer_state = (
                    self._outer.schema_changed,
                    self._outer.schema_types.copy(),
                    self._outer.schema_all,
                    self._outer.schema_initial,
                    self._outer.python_rows,
                )
                self._stack = stack.pop_all()
                return self._outer
            # Flush removes the seed. The unique key arbitrates concurrent
            # recreation; lock the resulting row before any source reads.
            IndexState.objects.using(self.using).select_for_update().get_or_create(key="global")
            # Another transaction cannot have durable in-flight work while we
            # own the global lock. Preserve nested owners and captures whose
            # caller atomic is still running; everything else is abandoned.
            live = tuple(
                item for item in _deferred.get() if item[2] in connections[item[0]].atomic_blocks
            )
            _deferred.set(live)
            preserved = [p.pass_id for p in _passes.get() if p.using == self.using]
            preserved.extend(pass_id for alias, pass_id, _ in live if alias == self.using)
            if self.resume_pass is not None:
                preserved.append(self.resume_pass)
            IndexWork.objects.using(self.using).exclude(pass_id__in=preserved).delete()
            _vacuum_model_terms(using=self.using)
            marker = IndexWork.objects.using(self.using).create(
                pass_id=0, kind="pass", phase="region"
            )
            self.pass_id = marker.pk
            IndexWork.objects.using(self.using).filter(pk=marker.pk).update(pass_id=self.pass_id)
            self._started = monotonic()
            self._token = _passes.set((*_passes.get(), self))
            self._stack = stack.pop_all()
            return self

    def load_program(self) -> IndexProgram:
        if self.program is None:
            if self.backend is None:
                from rebac.index.read import _backend

                self.backend = _backend()
            self.program = get_program(self.using, self.backend)
        return self.program

    def watches(self, model: type[models.Model], names: Iterable[str] | None = None) -> bool:
        return model_is_watched(self.load_program().watched, model, names)

    def work(self, *, phase: str | None = None) -> models.QuerySet[Any]:
        from rebac.models.index import IndexWork

        rows = IndexWork.objects.using(self.using).filter(pass_id=self.pass_id)
        return cast(models.QuerySet[Any], rows if phase is None else rows.filter(phase=phase))

    def add_terms(self, terms: models.QuerySet[Any], *, phase: str) -> int:
        from rebac.index.write import stream_create
        from rebac.models.index import IndexWork

        inserted = 0
        for kind, selection in (
            ("scope", terms.filter(relation__in=("", "$type"))),
            ("set", terms.exclude(relation__in=("", "$type"))),
        ):
            present = self.work(phase=phase).filter(kind=kind, term_id=OuterRef("pk"), node="")
            source = (
                selection.filter(~Exists(present))
                .order_by()
                .values(
                    pass_id=Value(self.pass_id, output_field=models.BigIntegerField()),
                    kind=Value(kind),
                    term_id=F("pk"),
                    node=Value(""),
                    phase=Value(phase),
                )
            )
            inserted += stream_create(
                source.distinct(),
                IndexWork,
                ("pass_id", "kind", "term_id", "node", "phase"),
                using=self.using,
            )
        self.python_rows += inserted
        return inserted

    def add_triples(self, triples: Iterable[tuple[str, str, str]], *, phase: str) -> None:
        from rebac.index.terms import intern
        from rebac.models.index import IndexTerm

        ids = intern(triples, using=self.using)
        self.python_rows += len(ids)
        self.add_terms(IndexTerm.objects.using(self.using).filter(pk__in=ids.values()), phase=phase)

    def capture_values(
        self, rows: models.QuerySet[Any], type_: str, attr: str, *, phase: str
    ) -> None:
        # identity_codec also handles relation paths through model metadata.
        from rebac.field_backing import _relation_path
        from rebac.index.codec import identity_codec
        from rebac.index.terms import intern_from
        from rebac.models.index import IndexTerm

        prefix, separator, terminal = attr.rpartition("__")
        model = _relation_path(rows.model, prefix)[0] if separator else rows.model
        codec = identity_codec(model, terminal)
        source = (
            rows.order_by()
            .values("pk")
            .annotate(type=Value(type_), object_id=codec.to_wire(attr), relation=Value(""))
            .filter(~Q(object_id="") & Q(object_id__isnull=False))
            .values("type", "object_id", "relation")
            .distinct()
        )
        self.python_rows += intern_from(source, using=self.using)
        self.add_terms(
            IndexTerm.objects.using(self.using).filter(
                type=type_, relation="", object_id__in=Subquery(source.values("object_id"))
            ),
            phase=phase,
        )

    def capture_old(
        self,
        *,
        model: type[models.Model] | None = None,
        pks: Iterable[Any] = (),
        queryset: models.QuerySet[Any] | None = None,
        tuples: Iterable[RelationshipTuple] = (),
    ) -> None:
        if queryset is not None:
            model = queryset.model
        self._capture(model=model, pks=pks, queryset=queryset, tuples=tuples, phase="old")
        # Preserve the seed sets while their old edges still exist.
        self.expand_region(phase="old")

    def snapshot_queryset(self, queryset: models.QuerySet[Any]) -> models.QuerySet[Any]:
        """Freeze primary keys before changing the owner's predicate."""
        from rebac.index.codec import identity_codec
        from rebac.index.terms import intern_from
        from rebac.index.write import stream_create
        from rebac.models.index import IndexTerm, IndexWork

        codec = identity_codec(queryset.model, "pk")
        type_ = "$model/" + queryset.model._meta.label_lower
        source = (
            queryset.order_by()
            .values("pk")
            .annotate(type=Value(type_), object_id=codec.to_wire("pk"), relation=Value(""))
            .values("type", "object_id", "relation")
            .distinct()
        )
        self.python_rows += intern_from(source, using=self.using)
        terms = IndexTerm.objects.using(self.using).filter(
            type=type_, object_id__in=Subquery(source.values("object_id"))
        )
        # The frozen set is this statement's rows, read before its SQL runs
        # and kept in the database under a per-statement tag: the gates decide
        # about exactly the rows the statement writes, a later statement of
        # the same pass does not inherit an earlier one's, and no primary-key
        # list ever travels through SQL parameters.
        self.statements += 1
        tag = f"statement:{self.statements}"
        self.python_rows += stream_create(
            terms.values(
                pass_id=Value(self.pass_id, output_field=models.BigIntegerField()),
                kind=Value("model"),
                term_id=F("pk"),
                node=Value(tag),
                phase=Value("old"),
            ),
            IndexWork,
            ("pass_id", "kind", "term_id", "node", "phase"),
            using=self.using,
        )
        frozen = IndexTerm.objects.using(self.using).filter(
            type=type_,
            pk__in=Subquery(
                self.work(phase="old").filter(kind="model", node=tag).values("term_id")
            ),
        )
        ids = frozen.annotate(column_pk=codec.to_column("object_id")).values("column_pk")
        return cast(
            models.QuerySet[Any],
            queryset.model._base_manager.using(self.using).filter(pk__in=Subquery(ids)),
        )

    def changed(
        self,
        *,
        model: type[models.Model] | None = None,
        pks: Iterable[Any] = (),
        tuples: Iterable[RelationshipTuple] = (),
        schema: bool = False,
    ) -> None:
        self.schema_changed |= schema
        if not schema:
            self._capture(model=model, pks=pks, tuples=tuples, phase="new")

    def _capture(
        self,
        *,
        model: type[models.Model] | None,
        pks: Iterable[Any],
        tuples: Iterable[RelationshipTuple],
        phase: str,
        queryset: models.QuerySet[Any] | None = None,
    ) -> None:
        from rebac._id import resource_id_attr
        from rebac.field_backing import (
            resolve_attribute_backing,
            resolve_const_backing,
            resolve_field_backing,
        )
        from rebac.resources import model_for_resource_type

        if model is not None and queryset is None:
            for pk_batch in self._batches(pks):
                self._capture(
                    model=model,
                    pks=(),
                    tuples=(),
                    phase=phase,
                    queryset=model._base_manager.using(self.using).filter(pk__in=pk_batch),
                )
            model = None

        for batch in batched(tuples, 200, strict=False):
            self.add_triples(
                (
                    triple
                    for tuple_ in batch
                    for triple in (
                        (tuple_.resource.resource_type, tuple_.resource.resource_id, ""),
                        (
                            tuple_.resource.resource_type,
                            tuple_.resource.resource_id,
                            tuple_.relation,
                        ),
                        *(
                            (
                                (tuple_.subject.subject_type, tuple_.subject.subject_id, ""),
                                (
                                    tuple_.subject.subject_type,
                                    tuple_.subject.subject_id,
                                    tuple_.subject.optional_relation,
                                ),
                            )
                            if tuple_.subject.optional_relation
                            else ()
                        ),
                    )
                ),
                phase=phase,
            )
        if model is None:
            return
        assert model._meta.concrete_model is not None
        rows = (
            queryset
            if queryset is not None
            else model._base_manager.using(self.using).filter(pk__in=pks)
        )
        # An MTI child's primary key need not be its parent's primary key.
        # Read the actual parent-link values while the old source still exists.
        for parent, link in model._meta.concrete_model._meta.parents.items():
            if parent is not None and link is not None:
                self._capture(
                    model=parent,
                    pks=(),
                    tuples=(),
                    phase=phase,
                    queryset=parent._base_manager.using(self.using).filter(
                        pk__in=Subquery(rows.order_by().values(link.attname))
                    ),
                )
        for definition in self.load_program().baseline.definitions:
            type_ = definition.resource_type
            source_model = model_for_resource_type(type_)
            if (
                source_model is not None
                and source_model._meta.concrete_model is model._meta.concrete_model
            ):
                self.capture_values(rows, type_, resource_id_attr(source_model), phase=phase)
            for relation in definition.relations:
                const = resolve_const_backing(definition, relation)
                if const is not None and const.filters:
                    for sources in self.reverse_sources(
                        const.source_model, tuple(const.filters), model, rows
                    ):
                        self.capture_values(
                            sources,
                            type_,
                            resource_id_attr(const.source_model),
                            phase=phase,
                        )
                attr = resolve_attribute_backing(definition, relation)
                if attr is not None:
                    for targets in self.reverse_sources(
                        attr.target_model, tuple(attr.filters), model, rows
                    ):
                        if attr.resource is None:
                            self.capture_values(targets, type_, attr.field.attname, phase=phase)
                        else:
                            self.add_triples(((type_, attr.resource, ""),), phase=phase)
                backing = resolve_field_backing(definition, relation)
                if backing is None:
                    continue
                for sources in self.reverse_sources(
                    backing.source_model, (backing.path, *backing.filters), model, rows
                ):
                    # Deliberately omit backing filters: transitions into and
                    # out of a filter must capture the same source identity.
                    # The edge belongs to its source. Its target's rows do not
                    # read incoming edges, so the target is not in the region.
                    self.capture_values(sources, type_, backing.source_id_attr, phase=phase)

    def reverse_sources(
        self,
        source: type[models.Model],
        paths: Sequence[str],
        changed: type[models.Model],
        rows: models.QuerySet[Any],
    ) -> Iterator[models.QuerySet[Any]]:
        """Resolve reverse dependencies in backing paths AND predicate paths."""
        prefixes: set[str] = set()
        if source._meta.concrete_model is changed._meta.concrete_model:
            prefixes.add("")
        from rebac.field_backing import _relation_path

        through_sources: set[tuple[str, str]] = set()
        through_fallback = False

        def visit(owner: type[models.Model], field: ModelField, prefix: str) -> None:
            nonlocal through_fallback
            target = getattr(field, "related_model", None)
            if not field.is_relation or not isinstance(target, type):
                return
            through = getattr(getattr(field, "remote_field", None), "through", None)
            through = through or getattr(field, "through", None)
            if (
                isinstance(through, type)
                and issubclass(through, models.Model)
                and through._meta.concrete_model is changed._meta.concrete_model
            ):
                source_keys = {
                    fk.attname
                    for fk in through._meta.fields
                    if isinstance(fk, models.ForeignKey)
                    and fk.remote_field.model._meta.concrete_model is owner._meta.concrete_model
                }
                if source_keys:
                    owner_prefix = prefix.rpartition("__")[0]
                    through_sources.update((owner_prefix, key) for key in source_keys)
                else:
                    through_fallback = True
            if (
                issubclass(target, models.Model)
                and target._meta.concrete_model is changed._meta.concrete_model
            ):
                prefixes.add(prefix)

        for path in sorted(paths):
            _relation_path(source, path, lookup=True, visit=visit)
        if through_fallback:
            yield source._base_manager.using(self.using).all()
        for prefix, key in sorted(through_sources):
            lookup = prefix + "__pk__in" if prefix else "pk__in"
            # Only the changed through rows' source keys are needed here.
            source_ids = set(rows.order_by().values_list(key, flat=True))
            for batch in self._batches(source_ids):
                yield source._base_manager.using(self.using).filter(**{lookup: batch})
        for prefix in sorted(prefixes):
            lookup = prefix + "__pk__in" if prefix else "pk__in"
            yield source._base_manager.using(self.using).filter(
                **{lookup: Subquery(rows.order_by().values("pk"))}
            )

    def expand_region(self, *, phase: str = "region") -> None:
        """Add the seed scope's sets, without closing over dependents."""
        from rebac.models.index import IndexTerm

        terms = IndexTerm.objects.using(self.using)
        scopes = terms.filter(
            pk__in=Subquery(self.work(phase=phase).filter(kind="scope").values("term_id"))
        )
        sets = terms.exclude(relation__in=("", "$type")).filter(
            Exists(scopes.filter(type=OuterRef("type"), object_id=OuterRef("object_id")))
        )
        self.add_terms(sets, phase=phase)

    def _set_region(self, ids: set[int]) -> None:
        from rebac.models.index import IndexTerm

        if ids == self._region_ids:
            return
        self.work(phase="region").delete()
        for batch in self._batches(ids):
            self.add_terms(IndexTerm.objects.using(self.using).filter(pk__in=batch), phase="region")
        self._region_ids = set(ids)

    def _batches(self, ids: Iterable[int], *, reserve: int = 0) -> Iterator[tuple[int, ...]]:
        """Id chunks that fit one statement; ``reserve`` is its other parameters."""
        limit = connections[self.using].features.max_query_params or 5000
        yield from batched(sorted(ids), max(1, min(5000, limit - 32 - reserve)), strict=False)

    @staticmethod
    def _key_filter(keys: Iterable[tuple[str, str]]) -> Q:
        selected = Q(pk__in=[])
        for type_, node in sorted(keys):
            selected |= Q(resource_type=type_, node=node)
        return selected

    def _payloads(
        self, keys: set[tuple[str, str]], scopes: set[int]
    ) -> dict[tuple[int, str], set[tuple[Any, ...]]]:
        from rebac.models.index import IndexCover

        result: dict[tuple[int, str], set[tuple[Any, ...]]] = {}
        if not scopes or not keys:
            return result
        selected = self._key_filter(keys)
        # The key filter binds a type and a node per stratum key.
        for batch in self._batches(scopes, reserve=2 * len(keys)):
            rows = IndexCover.objects.using(self.using).filter(selected, scope_id__in=batch)
            for scope, node, holder, site, expiry, condition_key, condition in rows.values_list(
                "scope_id", "node", "holder_id", "site", "expires_at", "condition_key", "condition"
            ).iterator(chunk_size=256):
                self.python_rows += 1
                result.setdefault((scope, node), set()).add(
                    (holder, site, expiry, condition_key, json.dumps(condition, sort_keys=True))
                )
        return result

    def _inputs(
        self, key: tuple[str, str]
    ) -> tuple[set[tuple[str, str]], set[tuple[str, str, str]], set[tuple[str, str]]]:
        """Same-scope, arrow-grant, and direct edge inputs of a node."""
        node = self.load_program().nodes[key]
        same: set[tuple[str, str]] = set()
        arrows: set[tuple[str, str, str]] = set()
        edges: set[tuple[str, str]] = set()
        if node.kind == "relation":
            return same, arrows, {(node.type, node.name)}
        if node.operands is not None:
            return {(node.type, name) for name in node.operands}, arrows, edges

        def visit(expr: Any) -> None:
            if isinstance(expr, PermRef):
                same.add((node.type, expr.name))
            elif isinstance(expr, PermArrow):
                edges.add((node.type, expr.via))
                arrows.update(
                    (type_, expr.target, expr.via)
                    for type_, name in node.deps
                    if name == expr.target
                )
            elif isinstance(expr, PermBinOp):
                visit(expr.left)
                visit(expr.right)

        if node.expr is not None:
            visit(node.expr)
        return same, arrows, edges

    def _input_scopes(
        self, key: tuple[str, str], changes: dict[tuple[str, str], set[int]]
    ) -> set[int]:
        from rebac.models.index import IndexEdge, IndexTerm

        same, arrows, _ = self._inputs(key)
        found = set().union(*(changes.get(dep, set()) for dep in same))
        for type_, target, via in sorted(arrows):
            changed = changes.get((type_, target), set())
            if not changed:
                continue
            edges = IndexEdge.objects.using(self.using).filter(
                resource_type=key[0], relation=via, target__type=type_
            )
            for batch in self._batches(changed):
                found.update(
                    edges.filter(target_id__in=batch).values_list("resource_id", flat=True)
                )
            if any(
                IndexTerm.objects.using(self.using).filter(pk__in=batch, relation="$type").exists()
                for batch in self._batches(changed)
            ):
                found.update(edges.values_list("resource_id", flat=True))
        return found

    def _member_payloads(self, sets: set[int]) -> dict[int, set[tuple[Any, ...]]]:
        from rebac.models.index import IndexMember

        result: dict[int, set[tuple[Any, ...]]] = {}
        for batch in self._batches(sets):
            rows = IndexMember.objects.using(self.using).filter(set_id__in=batch)
            for set_id, member, member_type, key, expiry, condition in rows.values_list(
                "set_id", "member_id", "member_type", "condition_key", "expires_at", "condition"
            ).iterator(chunk_size=256):
                self.python_rows += 1
                result.setdefault(set_id, set()).add(
                    (member, member_type, key, expiry, json.dumps(condition, sort_keys=True))
                )
        return result

    def _delete_members(self, sets: set[int]) -> int:
        from rebac.models.index import IndexMember

        return sum(
            IndexMember.objects.using(self.using).filter(set_id__in=batch).delete()[0]
            for batch in self._batches(sets)
        )

    def _containing_sets(self, members: set[int], *, edges: bool) -> set[int]:
        from rebac.models.index import IndexEdge, IndexMember, IndexTerm

        result: set[int] = set()
        for batch in self._batches(members):
            if edges:
                contained = IndexEdge.objects.using(self.using).filter(
                    subject_id__in=batch,
                    resource__type=OuterRef("type"),
                    resource__object_id=OuterRef("object_id"),
                    relation=OuterRef("relation"),
                )
                result.update(
                    IndexTerm.objects.using(self.using)
                    .exclude(relation__in=("", "$type"))
                    .filter(Exists(contained))
                    .values_list("pk", flat=True)
                )
            else:
                result.update(
                    IndexMember.objects.using(self.using)
                    .filter(member_id__in=batch)
                    .values_list("set_id", flat=True)
                )
        return result

    def _maintain_memberships(
        self, program: IndexProgram, seeds: set[int], removed_edges: set[tuple[int, str]]
    ) -> Stats:
        from rebac.index.derive import derive_memberships
        from rebac.index.project import Stats
        from rebac.models.index import IndexTerm

        stats = Stats()
        if not seeds:
            return stats
        source_ids = {scope for scope, _ in removed_edges}
        sources: dict[int, tuple[str, str]] = {}
        for batch in self._batches(source_ids):
            sources.update(
                (pk, (type_, object_id))
                for pk, type_, object_id in IndexTerm.objects.using(self.using)
                .filter(pk__in=batch)
                .values_list("pk", "type", "object_id")
            )
        removed_sets = {
            (*sources[scope], relation) for scope, relation in removed_edges if scope in sources
        }
        removal = False
        for batch in self._batches(seeds):
            if any(
                (type_, object_id, relation) in removed_sets
                for type_, object_id, relation in IndexTerm.objects.using(self.using)
                .filter(pk__in=batch)
                .values_list("type", "object_id", "relation")
            ):
                removal = True
                break
        if removal:
            region = set(seeds)
            frontier = set(seeds)
            while frontier:
                frontier = self._containing_sets(frontier, edges=False) - region
                region.update(frontier)
            self._set_region(region)
            stats.deleted += self._delete_members(region)
            stats.add(derive_memberships(program, using=self.using, region=self.pass_id))
            return stats
        pending = set(seeds)
        while pending:
            current = pending
            self._set_region(current)
            old = self._member_payloads(current)
            stats.deleted += self._delete_members(current)
            stats.add(derive_memberships(program, using=self.using, region=self.pass_id))
            new = self._member_payloads(current)
            changed = {set_id for set_id in current if old.get(set_id) != new.get(set_id)}
            pending = self._containing_sets(changed, edges=True) if changed else set()
        return stats

    def _recursive_region(self, stratum: tuple[tuple[str, str], ...], region: set[int]) -> set[int]:
        """Clear the connected SCC at once so old cycle rows cannot support themselves."""
        from rebac.models.index import IndexEdge, IndexTerm

        whole = set(stratum)
        pending = set(region)
        while pending:
            frontier: dict[int, str] = {}
            for batch in self._batches(pending):
                frontier.update(
                    IndexTerm.objects.using(self.using)
                    .filter(pk__in=batch)
                    .values_list("pk", "type")
                )
            pending = set()
            for key in stratum:
                _, arrows, _ = self._inputs(key)
                for type_, target, via in sorted(arrows):
                    if (type_, target) not in whole:
                        continue
                    targets = {pk for pk, scope_type in frontier.items() if scope_type == type_}
                    if not targets:
                        continue
                    edges = IndexEdge.objects.using(self.using).filter(
                        resource_type=key[0], relation=via, target__type=type_
                    )
                    for batch in self._batches(targets):
                        pending.update(
                            edges.filter(target_id__in=batch).values_list("resource_id", flat=True)
                        )
                    if any(
                        IndexTerm.objects.using(self.using)
                        .filter(pk__in=batch, relation="$type")
                        .exists()
                        for batch in self._batches(targets)
                    ):
                        pending.update(edges.values_list("resource_id", flat=True))
            pending.difference_update(region)
            region.update(pending)
        return region

    def _maintain_grants(
        self, program: IndexProgram, edges: dict[tuple[str, str], set[int]]
    ) -> Stats:
        from rebac.index.derive import derive_nodes
        from rebac.index.project import Stats
        from rebac.models.index import IndexCover, IndexTerm

        stats = Stats()
        changes: dict[tuple[str, str], set[int]] = {}
        for number, stratum in enumerate(program.strata):
            region: set[int] = set()
            for key in stratum:
                _, _, edge_inputs = self._inputs(key)
                for edge_key in edge_inputs:
                    region.update(edges.get(edge_key, set()))
                region.update(self._input_scopes(key, changes))
            if not region:
                continue
            recursive = any(program.nodes[key].recursive for key in stratum)
            if recursive:
                region = self._recursive_region(stratum, region)
            self._set_region(region)
            keys = set(stratum)
            scope_types: dict[int, str] = {}
            for batch in self._batches(region):
                scope_types.update(
                    IndexTerm.objects.using(self.using)
                    .filter(pk__in=batch)
                    .values_list("pk", "type")
                )
            old = self._payloads(keys, region)
            selected = self._key_filter(keys)
            for batch in self._batches(region, reserve=2 * len(keys)):
                stats.deleted += (
                    IndexCover.objects.using(self.using)
                    .filter(selected, scope_id__in=batch)
                    .delete()[0]
                )
            stats.add(
                derive_nodes(
                    program, using=self.using, region=self.pass_id, selected_stratum=number
                )
            )
            new = self._payloads(keys, region)
            for key in stratum:
                node = program.nodes[key]
                if node.operands is not None:
                    changes[key] = self._input_scopes(key, changes)
                else:
                    changes[key] = {
                        scope
                        for scope in region
                        if scope_types.get(scope) == key[0]
                        and old.get((scope, key[1]), set()) != new.get((scope, key[1]), set())
                    }
        return stats

    def finish(self, *, nested: bool = False) -> None:
        from rebac.index.project import Stats, project_edges
        from rebac.models.index import IndexTerm

        if self.completed_stats is not None:
            stats = self.completed_stats
            deleted, inserted, python_rows = stats.deleted, stats.inserted, stats.python_rows
        elif self.schema_changed:
            if nested:
                # The enclosing schema owner has published a new policy, but
                # its savepoint is still open; the outer finish rebuilds it.
                return
            from rebac.index.rebuild import _rebuild_locked

            self.program = get_program(self.using, self.backend)
            # A partial policy rebuild must include every source mutation
            # queued by the same owner, including revokes on unrelated types.
            pending = self.work().filter(kind__in=("scope", "set"))
            self.add_terms(
                IndexTerm.objects.using(self.using).filter(
                    pk__in=Subquery(pending.values("term_id"))
                ),
                phase="region",
            )
            selected = self.schema_types | set(
                pending.order_by().values_list("term__type", flat=True).distinct()
            )
            stats = _rebuild_locked(
                self, types=None if self.schema_all or not self.schema_types else sorted(selected)
            )
            deleted, inserted, python_rows = stats.deleted, stats.inserted, stats.python_rows
        elif self.work().exclude(kind="pass").exists():
            program = self.load_program()
            # Nested finishes leave work rows for the outer owner. Its next
            # projection widens the region outside _set_region's cache.
            self._region_ids = None
            terms = IndexTerm.objects.using(self.using)
            self.add_terms(
                terms.filter(
                    pk__in=Subquery(self.work().filter(kind__in=("scope", "set")).values("term_id"))
                ),
                phase="region",
            )
            projection = project_edges(program, using=self.using, region=self.pass_id, diff=True)
            self.expand_region()
            seed_sets = set(
                self.work(phase="region").filter(kind="set").values_list("term_id", flat=True)
            )
            membership = self._maintain_memberships(program, seed_sets, projection.removed_edges)
            nodes = self._maintain_grants(program, projection.changed_nodes)
            stats = Stats()
            for result in (projection, membership, nodes):
                stats.add(result)
            deleted, inserted, python_rows = stats.deleted, stats.inserted, stats.python_rows
        else:
            deleted = inserted = python_rows = 0
        types = (
            set(
                self.work()
                .filter(kind__in=("scope", "set"))
                .order_by()
                .values_list("term__type", flat=True)
                .distinct()
            )
            | self.schema_types
        )
        if (self.schema_changed or self.completed_stats is not None) and not types:
            types.update(key[0] for key in self.load_program().nodes)
        if not nested:
            self.work().delete()
            from rebac.index.rebuild import _vacuum_terms

            _vacuum_terms(
                using=self.using,
                defined=[d.resource_type for d in self.load_program().baseline.definitions],
            )
        logger.info(
            "Permission index maintained",
            extra={
                "pass_id": self.pass_id,
                "types": sorted(types),
                "deleted": deleted,
                "inserted": inserted,
                "python_rows": self.python_rows + python_rows,
                "duration": monotonic() - self._started,
            },
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                self._stack.__exit__(exc_type, exc, tb)
            else:
                with self._stack:
                    if self._outer is None and not self.deferred:
                        self.finish()
        finally:
            if exc_type is not None and self._outer is not None and self._outer_state is not None:
                # The savepoint rolls back durable work, but not Python flags.
                # A caught inner failure must not publish a rolled-back schema
                # change when the enclosing owner eventually exits.
                (
                    self._outer.schema_changed,
                    self._outer.schema_types,
                    self._outer.schema_all,
                    self._outer.schema_initial,
                    self._outer.python_rows,
                ) = self._outer_state
            if self._token is not None:
                _passes.reset(self._token)


@contextmanager
def maintain_tuples(
    *,
    written: Sequence[RelationshipTuple] = (),
    deleted_filter: RelationshipFilter | None = None,
    deleted: Sequence[RelationshipTuple] = (),
    using: str,
    backend: LocalBackend | None = None,
) -> Iterator[None]:
    from rebac.models import active_relationship_model
    from rebac.models.relationship import (
        RelationshipQuerySet,
        RelationshipRegistryQuerySet,
        projected_tuples,
    )

    nested = current_pass(using) is not None
    with IndexMaintenance(using=using, backend=backend) as maintenance:
        maintenance.capture_old(tuples=(*written, *deleted))
        if deleted_filter is not None:
            filters = deleted_filter.lookups()
            rows = cast(
                RelationshipQuerySet | RelationshipRegistryQuerySet,
                active_relationship_model().objects.using(using).filter(**filters),
            ).index_projection()
            # Streaming input is immediately materialized in IndexWork; there is
            # no queryset retained across the owner's DELETE.
            maintenance.capture_old(tuples=projected_tuples(rows))
        yield
        maintenance.changed(tuples=written)
        if nested:
            # A caller may check at the Zookie returned by this nested write
            # before its enclosing model owner exits. Derive now; the outer
            # owner can repeat the pass after its own final capture.
            maintenance.finish(nested=True)


@contextmanager
def model_write(
    *, model: type[models.Model], using: str, names: Iterable[str] | None = None
) -> Iterator[IndexMaintenance | None]:
    """Owned source writes always have an outer atomic block on their write alias.

    A write that touches no watched field must not take the index lock: a
    login's ``last_login`` save on a tracked user model would otherwise queue
    behind every maintenance pass. The watched-fields map is a cached program
    attribute, so this check needs neither the lock nor a database read.
    """
    from rebac.backends import backend
    from rebac.backends.local import LocalBackend

    outer = current_pass(using)
    active = outer.backend if outer is not None and outer.backend is not None else backend()
    if not isinstance(active, LocalBackend) or (
        outer is None
        and not (
            installed(using, active)
            and model_is_watched(get_program(using, active).watched, model, names)
        )
    ):
        with transaction.atomic(using=using):
            yield None
        return
    with IndexMaintenance(using=using, backend=active) as maintenance:
        yield maintenance if maintenance.watches(model, names) else None


@contextmanager
def tuple_owner(using: str, **captures: Any) -> Iterator[IndexMaintenance | None]:
    """Choose the local maintenance owner or an ordinary backend transaction."""
    from rebac.backends import backend
    from rebac.backends.local import LocalBackend

    outer = current_pass(using)
    active = outer.backend if outer is not None and outer.backend is not None else backend()
    if isinstance(active, LocalBackend) and (outer is not None or installed(using, active)):
        with IndexMaintenance(using=using, backend=active) as maintenance:
            if captures:
                maintenance.capture_old(**captures)
            yield maintenance
    else:
        with transaction.atomic(using=using):
            yield None
