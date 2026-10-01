"""Transaction owners and materialized old/new frontiers for the permission index."""

from __future__ import annotations

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
        self.membership_only = True
        self.python_rows = 0
        self._outer: IndexMaintenance | None = None
        self._outer_state: tuple[bool, set[str], bool, bool, int, bool] | None = None
        self._token: Token[tuple[IndexMaintenance, ...]] | None = None
        self._stack = ExitStack()
        self._started = 0.0
        self.deferred = False
        self._independent = independent
        self.completed_stats: Stats | None = None
        self.resume_pass = resume_pass

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
                    self._outer.membership_only,
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
        # Preserve dependents before a deleted/reparented edge disappears.
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
        )
        self.python_rows += intern_from(source, using=self.using)
        terms = IndexTerm.objects.using(self.using).filter(
            type=type_, object_id__in=Subquery(source.values("object_id"))
        )
        present = self.work(phase="old").filter(kind="model", term_id=OuterRef("pk"), node="")
        self.python_rows += stream_create(
            terms.filter(~Exists(present)).values(
                pass_id=Value(self.pass_id, output_field=models.BigIntegerField()),
                kind=Value("model"),
                term_id=F("pk"),
                node=Value(""),
                phase=Value("old"),
            ),
            IndexWork,
            ("pass_id", "kind", "term_id", "node", "phase"),
            using=self.using,
        )
        # The frozen set is this statement's rows, read before its SQL runs:
        # the gates decide about exactly the rows the statement writes, and a
        # later statement of the same pass does not inherit an earlier one's.
        statement_pks = list(queryset.order_by().values_list("pk", flat=True))
        return cast(
            models.QuerySet[Any],
            queryset.model._base_manager.using(self.using).filter(pk__in=statement_pks),
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

        if model is not None:
            self.membership_only = False

        for batch in batched(tuples, 200, strict=False):
            program = self.load_program()
            if any(
                (row.resource.resource_type, row.relation) not in program.userset_only_relations
                for row in batch
            ):
                self.membership_only = False
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
            yield source._base_manager.using(self.using).filter(**{lookup: source_ids})
        for prefix in sorted(prefixes):
            lookup = prefix + "__pk__in" if prefix else "pk__in"
            yield source._base_manager.using(self.using).filter(
                **{lookup: Subquery(rows.order_by().values("pk"))}
            )

    def expand_region(self, *, phase: str = "region") -> None:
        """Close the work set over what derivation reads.

        The rows at a scope are derived from the scope's edges, from the
        rows at the targets of its arrows, and from the type-level rows of
        those targets' types. The members of a set are derived from its edges
        and from the members of the sets it contains. A grant holds a set by
        reference: it does not change when the set's members do, so holders
        are not followed.
        """
        from rebac.index.terms import intern_from
        from rebac.models.index import IndexEdge, IndexMember, IndexTerm

        terms = IndexTerm.objects.using(self.using)
        arrows = Q(pk__in=[])
        for type_, via in sorted(self.load_program().arrow_vias):
            arrows |= Q(resource_type=type_, relation=via)
        while True:
            before = self.work(phase=phase).count()
            work = self.work(phase=phase).exclude(term_id=None)
            scopes = work.filter(kind="scope").values("term_id")
            sets = work.filter(kind="set").values("term_id")
            # The sets of a changed object: intern the ones its edges name,
            # then take every set of the object.
            edges = IndexEdge.objects.using(self.using).filter(resource_id__in=Subquery(scopes))
            self.python_rows += intern_from(
                edges.order_by()
                .values("relation", type=F("resource__type"), object_id=F("resource__object_id"))
                .values("type", "object_id", "relation"),
                using=self.using,
            )
            changed = terms.filter(
                pk__in=Subquery(scopes), type=OuterRef("type"), object_id=OuterRef("object_id")
            )
            self.add_terms(
                terms.filter(Exists(changed)).exclude(relation__in=("", "$type")), phase=phase
            )
            # The sets that contain a changed set.
            containers = IndexMember.objects.using(self.using).filter(member_id__in=Subquery(sets))
            self.add_terms(terms.filter(pk__in=Subquery(containers.values("set_id"))), phase=phase)
            # The scopes whose arrows read the rows of a changed scope.
            incoming = IndexEdge.objects.using(self.using).filter(
                arrows, target_id__in=Subquery(scopes)
            )
            self.add_terms(
                terms.filter(pk__in=Subquery(incoming.values("resource_id"))), phase=phase
            )
            # And those whose arrows read the type-level rows of a changed type.
            for type_ in (
                terms.filter(pk__in=Subquery(scopes), relation="$type")
                .values_list("type", flat=True)
                .distinct()
            ):
                incoming = IndexEdge.objects.using(self.using).filter(arrows, target__type=type_)
                self.add_terms(
                    terms.filter(pk__in=Subquery(incoming.values("resource_id"))), phase=phase
                )
            if self.work(phase=phase).count() == before:
                break

    def finish(self, *, nested: bool = False) -> None:
        from rebac.index.derive import derive_memberships, derive_nodes
        from rebac.index.project import Stats, project_edges
        from rebac.models.index import IndexCover, IndexEdge, IndexMember, IndexTerm

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
            terms = IndexTerm.objects.using(self.using)
            self.add_terms(
                terms.filter(
                    pk__in=Subquery(self.work().filter(kind__in=("scope", "set")).values("term_id"))
                ),
                phase="region",
            )
            # project_edges emits current sources; remove the old projection
            # only after its dependents have been captured in the old workset.
            edge_deleted = (
                IndexEdge.objects.using(self.using)
                .filter(resource_id__in=Subquery(self.work(phase="region").values("term_id")))
                .delete()[0]
            )
            projection = project_edges(program, using=self.using, region=self.pass_id)
            self.expand_region()
            ids = self.work(phase="region").values("term_id")
            covers = IndexCover.objects.using(self.using).filter(scope_id__in=Subquery(ids))
            deleted = 0 if self.membership_only else covers.delete()[0]
            deleted += (
                IndexMember.objects.using(self.using).filter(set_id__in=Subquery(ids)).delete()[0]
            )
            membership = derive_memberships(program, using=self.using, region=self.pass_id)
            nodes = (
                Stats()
                if self.membership_only
                else derive_nodes(program, using=self.using, region=self.pass_id)
            )
            stats = Stats(deleted=deleted + edge_deleted)
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
                    self._outer.membership_only,
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
