"""LocalBackend — permissions compiled to queries, and transactional tuple writes.

Proposed-object evaluation lives in preflight and the shared schema walker.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from threading import Lock
from typing import TYPE_CHECKING, Any
from weakref import WeakSet

from django.core.signals import setting_changed
from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper

from ..errors import SchemaError
from ..field_backing import (
    resolve_field_backing,
)
from ..links import NarrowingError, _published_for
from ..schema.ast import (
    AllowedSubject,
    AttributeBinding,
    ConstBinding,
    Definition,
    FieldBinding,
    Relation,
    Schema,
)
from ..schema.cache import SchemaSnapshot
from ..schema.cache import operation_scope as _schema_operation_scope
from ..schema.cache import schema_operation as _schema_operation
from ..schema.walker import (
    find_relation as _find_relation,
)
from ..schema.walker import (
    subject_allowed_by_relation as _subject_allowed_by_relation,
)
from ..types import (
    CheckItem,
    CheckResult,
    Consistency,
    ObjectRef,
    RelationshipFilter,
    RelationshipTuple,
    SubjectRef,
    Zookie,
)

if TYPE_CHECKING:
    from ..composition import TaggedComposition

_backend_registry_lock = Lock()
_db_loaded_backends: WeakSet[LocalBackend] = WeakSet()


@dataclass
class _SchemaFacts:
    """Whole-schema facts memoised per schema generation (see ``_schema_facts``)."""

    generation: int
    live_types: frozenset[str] | None = None


_relationship_generation = 0
# The transaction, per database alias, in which this thread wrote relationship
# rows that may still roll back: a fact read from them is not kept (proposal
# 0019). An ``atomic`` transaction is named by its outermost block.
_uncommitted_relationships = threading.local()
_MANUAL_TRANSACTION = object()


class _LoadedSchema(tuple[Schema, datetime | None]):
    """Keep the two-value loader API while retaining the baseline and its overrides."""

    def __new__(
        cls,
        schema: Schema,
        expires_at: datetime | None,
        baseline: Schema,
        overrides: tuple[Any, ...],
    ) -> _LoadedSchema:
        result = super().__new__(cls, (schema, expires_at))
        result.baseline = baseline
        result.overrides = overrides
        return result

    baseline: Schema
    overrides: tuple[Any, ...]


def _enforced_schema_errors(schema: Schema) -> list[str]:
    """Return contracts the local runtime must reject before evaluation.

    The backed relations' shape and the subject relations, chosen by the rule
    that found them, the same for a loaded policy as for an installed one.
    Other ``validate_schema`` findings (an unknown reference, a name read two
    ways) do not stop a policy from loading, whatever names it uses; nor does a
    name the schema language cannot spell: a stored policy that holds one keeps
    loading (``rebac.W013`` reports it), and only new input is refused
    (``_spelling_errors``, at ``set_schema`` and at stored-policy writes).
    """
    from ..schema.parser import backing_errors, subject_relation_errors

    return backing_errors(schema) + subject_relation_errors(schema)


def _spelling_errors(schema: Schema) -> list[str]:
    """The names of ``schema`` the schema language cannot spell, refused in new input."""
    from ..schema.parser import name_errors

    return name_errors(schema)


def _name_conflicts(schema: Schema) -> list[str]:
    """Where ``schema`` lets a name be read two ways (``rebac.schema.ast.name_conflicts``),
    refused in new input: a built-in's name as a relation, a definition or a subject
    type, and a name declared twice. A stored policy that holds one keeps loading
    (``rebac.W014``)."""
    from ..schema.ast import schema_name_conflicts

    return schema_name_conflicts(schema)


_UNENFORCED = "permissions are not enforced"


def _enforcing() -> bool:
    from ..actors import enforcing

    return enforcing()


def _every_id(resource_type: str, using: str) -> list[str]:
    """Every object of ``resource_type``, as ``accessible`` answers when nothing is enforced.

    The rows of a type that stores rows; for a type whose objects exist only in
    relationships, the ids relationships name as resources.
    """
    from .._id import resource_id_attr
    from ..models import active_relationship_model
    from ..resources import model_for_resource_type, stores_rows

    model = model_for_resource_type(resource_type)
    if model is not None and stores_rows(model):
        keys = (
            model._base_manager.using(using)
            .order_by()
            .values_list(resource_id_attr(model), flat=True)
        )
        return [str(key) for key in keys]
    named: Any = active_relationship_model().objects.using(using)
    rows = (
        named.filter(resource_type=resource_type).order_by().values_list("resource_id", flat=True)
    )
    return sorted({str(value) for value in rows})


def _candidates(ids: Iterable[str]) -> tuple[str, ...]:
    """The candidate ids of ``held``, a ``None`` read as the empty id, as ``ObjectRef`` reads it."""
    return tuple("" if value is None else value for value in ids)


class LocalBackend:
    """Evaluates permissions as queries over the application's own tables.

    Two entry points read permissions in bulk: ``accessible()`` enumerates the
    ids of a type an actor holds, and ``held()`` checks supplied candidate ids.
    """

    kind = "local"

    def has_access(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> bool:
        """Boolean shorthand for the three-state permission check."""
        return self.check_access(
            subject=subject,
            action=action,
            resource=resource,
            context=context,
            consistency=consistency,
            at_zookie=at_zookie,
        ).allowed

    @staticmethod
    def _validate_consistency(consistency: Consistency | None, at_zookie: Zookie | None) -> None:
        """Validate read options without filtering away newer relationship rows.

        Local reads use the current state visible to the Django connection.
        Row write timestamps cannot reconstruct historical updates/deletes.
        """
        if consistency is Consistency.AT_EXACT_SNAPSHOT:
            raise ValueError("LocalBackend does not support exact historical snapshots")
        if at_zookie is None:
            return
        if at_zookie.backend != "local":
            raise ValueError(
                f"LocalBackend cannot consume a Zookie from backend "
                f"{at_zookie.backend!r}. Drain or translate the token at "
                f"the boundary where backends switched."
            )
        try:
            int(at_zookie.token)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"LocalBackend Zookie token must be a numeric xid; got {at_zookie.token!r}"
            ) from exc

    def __init__(self) -> None:
        self._schema_lock = Lock()
        # Signals can run while their transaction holds database write locks.
        # They must not wait on a loader that might be waiting on that database.
        self._schema_invalidation_lock = Lock()
        self._schema: Schema | None = None
        self._schema_is_manual = False
        self._manual_revision = ""
        self._schema_snapshots: dict[tuple[str, str], SchemaSnapshot] = {}
        self._schema_loads: dict[tuple[str, str], Future[SchemaSnapshot | None]] = {}
        self._schema_generation = 0
        self._schema_invalidation_generation = 0
        # Keyed by (generation, inherits): the facts of a policy narrowed by
        # REBAC_INHERIT_THROUGH_LINKS are not those of the published one.
        self._schema_facts_memo: dict[tuple[int, bool], _SchemaFacts] = {}
        # Proposal 0023: the narrowed schema of each published snapshot schema,
        # by identity, and the narrowing of each composed policy, by its
        # baseline's identity and its override rows; a refusal is kept too.
        self._narrowed: OrderedDict[int, tuple[Schema, Schema]] = OrderedDict()
        self._compositions: OrderedDict[
            tuple[Any, ...], tuple[Schema, TaggedComposition | NarrowingError]
        ] = OrderedDict()
        # Counter used as a stable monotonic xid on backends (e.g. SQLite test
        # mode) without `txid_current()`.
        self._xid_counter = 0
        self._last_xid = 0
        with _backend_registry_lock:
            _db_loaded_backends.add(self)

    # ---------- Schema management ----------

    def set_schema(self, schema: Schema) -> None:
        """Install an in-memory schema in place of the stored policy
        (``rebac.testing.install_schema``).

        Its names must be ones the schema language can spell, each read one way
        (``rebac.schema.ast.name_conflicts``), whoever built it.
        """
        schema_errors = (
            _spelling_errors(schema) + _name_conflicts(schema) + _enforced_schema_errors(schema)
        )
        if schema_errors:
            raise SchemaError("; ".join(schema_errors))
        from ..schema.serialization import digest

        revision = digest(repr(schema))[:32]
        with self._schema_lock, self._schema_invalidation_lock:
            self._schema = schema
            self._manual_revision = revision
            self._schema_is_manual = True
            self._schema_snapshots.clear()
            self._schema_facts_memo.clear()
            self._narrowed.clear()
            self._compositions.clear()
            self._schema_generation += 1

    def schema(self) -> Schema:
        """The published effective policy: what ``rebac build-zed`` exports.

        Whatever ``REBAC_INHERIT_THROUGH_LINKS`` says, so that no caller lists a
        permission the narrowed policy generates (proposal 0023). Listing,
        rendering and validating against declared names read this one. The
        policy decisions are made with is ``evaluated_schema()``.
        """
        return self._load_schema_snapshot().schema

    def _published_schema(self) -> Schema:
        """The published effective policy, whatever ``REBAC_INHERIT_THROUGH_LINKS`` says."""
        return self._load_schema_snapshot().schema

    def evaluated_schema(self) -> Schema:
        """The policy decisions are made with, for code that proves things about it.

        With ``REBAC_INHERIT_THROUGH_LINKS = True`` it is the published policy,
        the object ``schema()`` returns. With ``False`` it is the published policy
        narrowed (``rebac.links``, proposal 0023): the object decisions read (the
        compiled policy's ``effective`` composition, which ``check_new`` walks and
        the residual evaluator reads, and which statements are compiled from unless
        override rows are selected, all but an expired recaveat; then they are
        compiled from the narrowing of the composition that keeps those rows'
        deadlines), kept per schema snapshot, so calls under one snapshot return
        one object, narrowed once, and a policy change or a change of the setting
        returns another. The field gates
        find the fields they gate in ``schema()``, the published policy, and decide
        them under the evaluated one, as every decision is: a gate's
        ``read__<field>`` or ``write__<field>`` is a declared name, and both policies
        declare the same names, so both find the same gates.

        Experimental (proposal 0023), outside the stable public API, and a
        ``LocalBackend`` convenience with no SpiceDB counterpart: the evaluated
        policy is not a ``.zed`` schema.

        - It is the object decisions read: never mutate it.
        - It holds generated permissions (``lent(P)``, ``excluded(P)``;
          ``rebac.links.is_generated(name)``): internal versions of declared
          permissions, not actions. Never show one to a user or pass one to an
          entry, which answers it as the published policy answers an unknown
          action. List, render and validate against declared names with
          ``schema()``.
        - A proof made from its text holds only while the policy carries no
          override: an override's arm holds until its deadline and a recaveat
          replaces a caveat's expression, neither of which the text shows. The
          library's prover proves nothing while any override row is stored, and
          ``rebac.models.SchemaOverride.objects.using(<the schema's database>)
          .exists()`` tells a consumer the same. Nor does a proof hold on a type
          that lets a name be read two ways (``rebac.schema.ast.name_conflicts``,
          ``rebac.W014``).
        - With the setting ``False`` and a narrowed policy that cannot be built it
          raises ``NarrowingError`` (``rebac.E025``), and with a setting that is
          not a bool ``ImproperlyConfigured`` (``rebac.E028``), as a decision does.
        """
        return self._schema_snapshot().schema

    def _manual_schema_revision(self) -> str:
        """The digest of the manual schema; set_schema performs no I/O."""
        with self._schema_lock:
            return self._manual_revision

    def _schema_snapshot(self, known_revision: str | None = None) -> SchemaSnapshot:
        """The policy every decision is made with: the one place the setting applies.

        ``REBAC_INHERIT_THROUGH_LINKS`` is read at each call (proposal 0023).
        With ``False`` the snapshot's schema is the published one narrowed
        (``rebac.links``), and its ``baseline`` is the published policy the
        compiled policy is composed from. Snapshots are cached and pinned as
        published, so a change of the setting is seen by the next call.
        """
        if known_revision is None:
            snapshot = self._load_schema_snapshot()
        else:
            snapshot = self._load_schema_snapshot(known_revision)
        from ..links import inherits_through_links

        if inherits_through_links():
            return snapshot
        return snapshot._replace(
            schema=self._narrowed_schema(snapshot),
            baseline=snapshot.baseline if snapshot.baseline is not None else snapshot.schema,
            inherits=False,
        )

    def _narrowed_schema(self, snapshot: SchemaSnapshot) -> Schema:
        """The snapshot's schema without inheritance through links, kept by its identity.

        The narrowing of the snapshot's own composition, as published: no
        override row is selected again. The compiled policy reads this same
        object as its ``effective`` composition, so ``check_new`` and the
        residual evaluator read one narrowing.
        """
        schema = snapshot.schema
        with self._schema_lock:
            kept = self._narrowed.get(id(schema))
            if kept is not None and kept[0] is schema:
                self._narrowed.move_to_end(id(schema))
                return kept[1]
        result = self._narrowed_composition(schema, ()).schema
        with self._schema_lock:
            self._narrowed[id(schema)] = (schema, result)
            while len(self._narrowed) > 16:
                self._narrowed.popitem(last=False)
        return result

    def _narrowed_composition(self, baseline: Schema, rows: Iterable[Any]) -> TaggedComposition:
        """``baseline`` composed with the override ``rows``, narrowed: once per composition.

        Kept by the baseline's identity and the rows' content, so that a policy
        prepared afresh at each use while an override deadline is ahead narrows
        once. A refusal the narrowing makes about the policy is kept and raised
        again; an unexpected failure is raised and not kept, so the next
        operation narrows again.
        """
        from ..composition import compose_tagged
        from ..links import NarrowingError, narrowed_tagged

        rows = list(rows)
        key = (
            id(baseline),
            tuple(
                (
                    getattr(row, "pk", None),
                    row.kind,
                    row.expression,
                    row.expires_at,
                    getattr(row, "created_at", None),
                    getattr(row, "target_ct_id", None),
                    getattr(row, "target_pk", None),
                )
                for row in rows
            ),
        )
        with self._schema_lock:
            kept = self._compositions.get(key)
            if kept is not None and kept[0] is baseline:
                self._compositions.move_to_end(key)
                found = kept[1]
                if isinstance(found, NarrowingError):
                    raise found
                return found
        composed = compose_tagged(baseline, rows)
        result: TaggedComposition | NarrowingError
        try:
            # Classifying relations resolves model fields: outside the lock.
            result = narrowed_tagged(composed)
        except NarrowingError as exc:
            result = exc
        if isinstance(result, NarrowingError) and not result.kept:
            raise result
        with self._schema_lock:
            self._compositions[key] = (baseline, result)
            while len(self._compositions) > 32:
                self._compositions.popitem(last=False)
        if isinstance(result, NarrowingError):
            raise result
        return result

    def _load_schema_snapshot(self, known_revision: str | None = None) -> SchemaSnapshot:
        """The schema at the published revision, pinned to the scope that asks.

        ``known_revision`` is a revision the caller has just read on the
        schema's own alias: the first attempt uses it instead of reading it
        again.
        """
        from django.db import connections
        from django.utils import timezone

        from ..evaluator import current_evaluator
        from ..models import SchemaDefinition
        from ..models.generation import SchemaGeneration

        with self._schema_lock:
            if self._schema_is_manual and self._schema is not None:
                return SchemaSnapshot(
                    self._schema,
                    None,
                    self._schema_generation,
                    0,
                    self._manual_revision,
                )
        with self._schema_invalidation_lock:
            invalidation = self._schema_invalidation_generation
        connection = connections[SchemaDefinition.objects.db]
        evaluator = current_evaluator()
        operation = _schema_operation_scope.get()
        # A rollback has no library-owned notification. Transactional pins
        # therefore live for one operation, including reused Atomic blocks.
        scoped = connection.get_autocommit() and not connection.in_atomic_block
        if evaluator is not None and not scoped:
            # A transactional read can see a revision that later rolls back.
            # Refresh the evaluator pin on its next autocommit operation.
            evaluator._schema_scope.snapshots(connection).clear()
        scope = evaluator._schema_scope if evaluator is not None and scoped else operation
        pins = scope.snapshots(connection) if scope is not None else None
        operation_pins = operation.snapshots(connection) if operation is not None else None
        for candidate in (pins, operation_pins):
            snapshot = candidate.get(self) if candidate is not None else None
            if (
                snapshot is not None
                and snapshot.invalidation_generation == invalidation
                and (snapshot.expires_at is None or snapshot.expires_at > timezone.now())
            ):
                return snapshot

        # Database I/O and waiting for another loader happen outside the process
        # lock. An evaluator validates once per scope / transaction boundary;
        # unscoped operations validate on every call.
        for attempt in range(3):
            with self._schema_invalidation_lock:
                invalidation = self._schema_invalidation_generation
            if attempt == 0 and known_revision:
                revision: str | None = known_revision
            else:
                revision = SchemaGeneration.objects.revision(connection.alias)
            if revision is None:
                with self._schema_lock:
                    self._evict_schema_alias(connection.alias)
                loaded = self._load_schema_from_db(connection.alias)
                schema, expires_at = loaded
                snapshot = SchemaSnapshot(
                    schema,
                    expires_at,
                    -1,
                    invalidation,
                    using=connection.alias,
                    baseline=getattr(loaded, "baseline", None),
                    overrides=getattr(loaded, "overrides", ()),
                )
                # Share nested reads for ONE operation, never an evaluator scope
                # or a decision cache. Later operations read all live schema rows.
                if operation_pins is not None:
                    operation_pins[self] = snapshot
                return snapshot
            key = (connection.alias, revision)
            with self._schema_lock:
                snapshot = self._schema_snapshots.get(key)
                if snapshot is not None and (
                    snapshot.expires_at is None or snapshot.expires_at > timezone.now()
                ):
                    pending = None
                    owner = False
                else:
                    pending = self._schema_loads.get(key)
                    owner = pending is None
                    if pending is None:
                        pending = Future()
                        self._schema_loads[key] = pending
            if pending is not None:
                if owner:
                    try:
                        try:
                            loaded = self._load_schema_from_db(connection.alias)
                            schema, expires_at = loaded
                        except SchemaError:
                            if self._read_schema_revision(connection) == revision:
                                raise
                            snapshot = None
                        else:
                            after = SchemaGeneration.objects.revision(connection.alias)
                            snapshot = None
                            if after == revision:
                                with self._schema_lock, self._schema_invalidation_lock:
                                    if invalidation == self._schema_invalidation_generation:
                                        self._evict_schema_alias(connection.alias)
                                        self._schema_generation += 1
                                        snapshot = SchemaSnapshot(
                                            schema,
                                            expires_at,
                                            self._schema_generation,
                                            0,
                                            revision,
                                            connection.alias,
                                            getattr(loaded, "baseline", None),
                                            getattr(loaded, "overrides", ()),
                                        )
                                        self._schema_snapshots[key] = snapshot
                        pending.set_result(snapshot)
                    except BaseException as exc:
                        pending.set_exception(exc)
                        raise
                    finally:
                        with self._schema_lock:
                            self._schema_loads.pop(key, None)
                else:
                    snapshot = pending.result()
            with self._schema_invalidation_lock:
                if invalidation != self._schema_invalidation_generation:
                    continue
            if snapshot is not None:
                pin = snapshot._replace(invalidation_generation=invalidation)
                if pins is not None:
                    pins[self] = pin
                return pin
        # Do not retain the final fallback, but fence its schema with the
        # revision read on both sides of the load. A loader retry miss alone
        # must not deny otherwise stable reads; a changing policy fails closed.
        before = SchemaGeneration.objects.revision(connection.alias)
        loaded = self._load_schema_from_db(connection.alias)
        schema, expires_at = loaded
        final = SchemaGeneration.objects.revision(connection.alias)
        if before != final:
            raise SchemaError("Schema changed throughout loading; retry the permission operation.")
        return SchemaSnapshot(
            schema,
            expires_at,
            -1,
            invalidation,
            revision=final,
            using=connection.alias,
            baseline=getattr(loaded, "baseline", None),
            overrides=getattr(loaded, "overrides", ()),
        )

    def _evict_schema_alias(self, alias: str) -> None:
        """Caller holds _schema_lock; in-flight scope pins own their old trees."""
        for key in list(self._schema_snapshots):
            if key[0] == alias:
                snapshot = self._schema_snapshots.pop(key)
                for inherits in (True, False):
                    self._schema_facts_memo.pop((snapshot.generation, inherits), None)

    def _read_schema_revision(self, connection: BaseDatabaseWrapper) -> str | None:
        """The published policy revision on the connection's alias."""
        from ..models.generation import SchemaGeneration

        return SchemaGeneration.objects.revision(connection.alias)

    def _cache_generation(self, resource_type: str) -> tuple[Any, ...] | None:
        """Return a decision generation for ``resource_type``, or ``None`` to bypass caching.

        Decisions are not cached inside a transaction (a rollback would leave
        stale answers), when relationships can expire without a write, or when
        a decision about the resource type reads a model column
        (``live_backed_resource_types``). Expired schema snapshots refresh
        before a cache key is reused; warm scope hits perform no SQL.
        """
        from django.db import connections

        from ..models import SchemaDefinition, active_relationship_model

        candidates = (
            connections[active_relationship_model().objects.db],
            connections[SchemaDefinition.objects.db],
        )
        from ..snapshot import covers

        for candidate in candidates:
            if (
                candidate.in_atomic_block
                or (candidate.connection is not None and not candidate.get_autocommit())
            ) and not covers(candidate.alias):
                # A read snapshot has nothing to roll back: decisions keep inside it.
                return None
        snapshot = self._schema_snapshot()
        if snapshot.generation == -1 or (snapshot.revision is None and not self._schema_is_manual):
            return None
        live_types = self._schema_facts(snapshot).live_types
        assert live_types is not None
        if resource_type in live_types:
            return None
        if any(
            relation.with_expiration
            for definition in snapshot.schema.definitions
            for relation in definition.relations
        ):
            return None
        # A decision of the narrowed policy (proposal 0023) is not one of the published policy.
        return snapshot.generation, _relationship_generation, snapshot.inherits

    def _schema_facts(self, snapshot: SchemaSnapshot) -> _SchemaFacts:
        """Whole-schema facts derived once per schema generation and policy."""
        from ..schema.introspection import live_backed_resource_types

        key = (snapshot.generation, snapshot.inherits)
        with self._schema_lock:
            cached = self._schema_facts_memo.get(key)
            if cached is not None and cached.live_types is not None:
                return cached
            facts = cached if cached is not None else _SchemaFacts(snapshot.generation)
            facts.live_types = live_backed_resource_types(snapshot.schema)
            current = (snapshot.generation == self._schema_generation) or any(
                s.generation == snapshot.generation for s in self._schema_snapshots.values()
            )
            if snapshot.generation != -1 and current:
                self._schema_facts_memo[key] = facts
            return facts

    def mark_schema_stale(self) -> None:
        """Discard compiled facts and invalidate DB-loaded schema snapshots."""
        # Loaders perform no database I/O under this lock, so a signal holding
        # database write locks can safely evict shared snapshots here.
        with self._schema_lock, self._schema_invalidation_lock:
            self._schema_facts_memo.clear()
            self._narrowed.clear()
            self._compositions.clear()
            if not self._schema_is_manual:
                self._schema_invalidation_generation += 1
                self._schema_snapshots.clear()

    def _load_schema_from_db(
        self, using: str, *, overrides: Iterable[Any] | None = None
    ) -> tuple[Schema, datetime | None]:
        from django.db.models import Prefetch
        from django.utils import timezone

        from ..composition import compose
        from ..models import (
            SchemaCaveat,
            SchemaDefinition,
            SchemaOverride,
            SchemaPermission,
            SchemaRelation,
        )
        from ..schema.ast import (
            Caveat,
            CaveatParam,
            Definition,
            Permission,
            Relation,
            Schema,
            backing_from_dict,
        )
        from ..schema.parser import parse_permission_expression

        # Bake the per-relation order_by into Prefetch so the prefetch cache
        # is reused; a bare `d.relations.all().order_by(...)` per definition
        # is N+1 on schema load.
        defs: list[Definition] = []
        defs_qs = (
            SchemaDefinition.objects.using(using)
            .prefetch_related(
                Prefetch(
                    "relations", queryset=SchemaRelation.objects.using(using).order_by("name")
                ),
            )
            .prefetch_related(
                Prefetch(
                    "permissions", queryset=SchemaPermission.objects.using(using).order_by("name")
                ),
            )
            .order_by("resource_type")
        )
        for d in defs_qs:
            relations = []
            for r in d.relations.all():
                allowed = tuple(
                    AllowedSubject(
                        type=item["type"],
                        relation=item.get("relation", ""),
                        wildcard=item.get("wildcard", False),
                        with_caveat=item.get("with_caveat", ""),
                        id=item.get("id", ""),
                    )
                    for item in (r.allowed_subjects or [])
                )
                try:
                    backing = backing_from_dict(r.backing)
                except (TypeError, ValueError, KeyError) as exc:
                    raise SchemaError(
                        f"{d.resource_type}#{r.name}: invalid relation backing: {exc}"
                    ) from exc
                relations.append(Relation(r.name, allowed, r.with_expiration, backing))
            permissions: list[Permission] = []
            for p in d.permissions.all():
                expr = parse_permission_expression(p.expression)
                permissions.append(Permission(p.name, expr, p.expression))
            defs.append(Definition(d.resource_type, tuple(relations), tuple(permissions)))

        caveats = []
        for c in SchemaCaveat.objects.using(using).order_by("name"):
            params = tuple(CaveatParam(p["name"], p["type"]) for p in (c.params or []))
            caveats.append(Caveat(c.name, params, c.expression))

        baseline = Schema(definitions=defs, caveats=caveats)

        # Tier-2: apply SchemaOverride composition. `compose()` is the
        # single source of determinism (it re-sorts disables by
        # (created_at, pk) per kind), so the loader-side order_by is just
        # cosmetic; we keep it for readable EXPLAIN plans.
        # The effective schema composes only the overrides active now; an
        # expired override must not grant. The compiler, though, reads EVERY
        # override row, expired ones included, from ``overrides``: a timed
        # disable or tighten carries its deadline into the statement and
        # becomes the identity when the statement's clock passes it.
        all_overrides = (
            list(
                SchemaOverride.objects.using(using)
                .select_related("target_ct")
                .order_by("kind", "created_at", "pk")
            )
            if overrides is None
            else list(overrides)
        )
        now = timezone.now()
        selected_overrides = [
            row for row in all_overrides if row.expires_at is None or row.expires_at > now
        ]
        from ..composition import split_stale_overrides

        selected_overrides, _stale = split_stale_overrides(baseline, selected_overrides)
        expires_at = min(
            (row.expires_at for row in selected_overrides if row.expires_at is not None),
            default=None,
        )
        effective = compose(baseline, selected_overrides)
        schema_errors = _enforced_schema_errors(effective)
        if schema_errors:
            raise SchemaError("; ".join(schema_errors))
        return _LoadedSchema(effective, expires_at, baseline, tuple(all_overrides))

    # ---------- Public API ----------
    #
    # Every entry that takes an action resolves it against the published policy
    # (``rebac.links._published_for``): a permission the policy without
    # inheritance through links generates is not an action (proposal 0023).

    @_schema_operation
    def check_access(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> CheckResult:
        with _published_for(action):
            return self._check_access(
                subject=subject,
                action=action,
                resource=resource,
                context=context,
                consistency=consistency,
                at_zookie=at_zookie,
            )

    def _check_access(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> CheckResult:
        """``check_access`` for any permission of the evaluated policy, a generated one included.

        For the proposed-object walk (``rebac.preflight``), whose arrow hops
        name what the evaluated policy names.
        """
        if not _enforcing():
            return CheckResult.has(reason=_UNENFORCED)
        self._validate_consistency(consistency, at_zookie)
        from ..compile.read import check
        from ..models import active_relationship_model

        return check(
            backend=self,
            resource=resource,
            action=action,
            actor=subject,
            context=context,
            using=active_relationship_model().objects.db,
            fresh=consistency is not None or at_zookie is not None,
        )

    def check_bulk_permissions(
        self,
        items: Iterable[CheckItem],
        *,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> list[CheckResult]:
        if not _enforcing():
            return [CheckResult.has(reason=_UNENFORCED) for _item in items]
        self._validate_consistency(consistency, at_zookie)
        from ..compile.read import check_many
        from ..links import is_generated
        from ..models import active_relationship_model

        listed = list(items)
        using = active_relationship_model().objects.db
        asked = [index for index, item in enumerate(listed) if is_generated(item.action)]
        if not asked:
            return check_many(backend=self, items=listed, using=using)
        # A generated name is the published policy's to answer, as an unknown action.
        answers: list[CheckResult | None] = [None] * len(listed)
        rest = [index for index in range(len(listed)) if index not in set(asked)]
        for indexes in (rest, asked):
            if not indexes:
                continue
            with _published_for([listed[index].action for index in indexes]):
                found = check_many(
                    backend=self, items=[listed[index] for index in indexes], using=using
                )
            for index, answer in zip(indexes, found, strict=True):
                answers[index] = answer
        return [answer for answer in answers if answer is not None]

    @_schema_operation
    def queryset_filter(
        self,
        *,
        model: type[models.Model],
        subject: SubjectRef,
        action: str,
        using: str,
    ) -> models.Q:
        """The compiled scope of ``model`` rows; no row for a model without a resource type."""
        if not _enforcing():
            return models.Q()
        from ..compile.read import scope_q

        with _published_for(action):
            return scope_q(backend=self, model=model, action=action, actor=subject, using=using)

    @_schema_operation
    def permission_expression(
        self,
        *,
        model: type[models.Model],
        subject: SubjectRef,
        action: str,
        using: str,
        identity: models.Expression,
    ) -> models.Expression:
        if not _enforcing():
            return models.Value(True, output_field=models.BooleanField())
        from ..compile.read import scope_expression

        with _published_for(action):
            return scope_expression(
                backend=self,
                model=model,
                action=action,
                actor=subject,
                using=using,
                identity=identity,
            )

    @_schema_operation
    def accessible(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> Iterable[str]:
        from ..compile.read import accessible_ids
        from ..models import active_relationship_model

        if not _enforcing():
            return _every_id(resource_type, active_relationship_model().objects.db)
        self._validate_consistency(consistency, at_zookie)
        with _published_for(action):
            return list(
                accessible_ids(
                    backend=self,
                    resource_type=resource_type,
                    action=action,
                    actor=subject,
                    using=active_relationship_model().objects.db,
                    context=context,
                    fresh=consistency is not None or at_zookie is not None,
                )
            )

    @_schema_operation
    def held(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        ids: Iterable[str],
        context: dict[str, Any] | None = None,
        using: str | None = None,
    ) -> set[str]:
        """The candidate ids on which the subject holds the action."""
        from .. import walk
        from ..compile.read import held
        from ..models import active_relationship_model

        ids = _candidates(ids)
        if not _enforcing():
            return {value for value in ids if value}
        alias = using or active_relationship_model().objects.db
        with _published_for(action):
            settled: set[str] = set()
            if walk.enabled():
                # What small lookups on the named rows answer; the compiled statement decides the rest.
                ids = tuple(dict.fromkeys(ids))
                settled, answered = walk.decided(
                    backend=self,
                    resource_type=resource_type,
                    action=action,
                    actor=subject,
                    ids=ids,
                    using=alias,
                    context=context,
                )
                ids = tuple(value for value in ids if value not in answered)
                if not ids:
                    return settled
            return settled | held(
                backend=self,
                resource_type=resource_type,
                action=action,
                actor=subject,
                ids=ids,
                using=alias,
                context=context,
            )

    @_schema_operation
    def held_many(
        self,
        *,
        subject: SubjectRef,
        actions: Iterable[str],
        resource_type: str,
        ids: Iterable[str],
        context: dict[str, Any] | None = None,
        using: str | None = None,
    ) -> dict[str, set[str]]:
        """The held candidate ids per action, equal permissions decided once."""
        from .. import walk
        from ..compile.read import held_many
        from ..links import is_generated

        if isinstance(actions, str):
            raise TypeError("actions must be an iterable of names, not a bare string")
        names = tuple(dict.fromkeys(actions))
        ids = _candidates(ids)
        if not _enforcing():
            candidates = {value for value in ids if value}
            return {name: set(candidates) for name in names}
        asked = tuple(name for name in names if is_generated(name))
        if asked and len(asked) < len(names):
            # A generated name is the published policy's to answer, as an unknown action.
            ids = tuple(ids)
            answers = self.held_many(
                subject=subject,
                actions=[name for name in names if name not in asked],
                resource_type=resource_type,
                ids=ids,
                context=context,
                using=using,
            )
            answers.update(
                self.held_many(
                    subject=subject,
                    actions=asked,
                    resource_type=resource_type,
                    ids=ids,
                    context=context,
                    using=using,
                )
            )
            return {name: answers[name] for name in names}
        from ..models import active_relationship_model

        alias = using or active_relationship_model().objects.db
        with _published_for(names):
            answers = {}
            if walk.enabled() and names:
                # As ``held``: the walk's answers first, the compiled statement for what is open.
                ids = tuple(dict.fromkeys(ids))
                walked = walk.decided_many(
                    backend=self,
                    resource_type=resource_type,
                    actions=names,
                    actor=subject,
                    ids=ids,
                    using=alias,
                    context=context,
                )
                answers = {name: set(walked[name][0]) for name in names}
                open_ids = {
                    value for name in names for value in ids if value not in walked[name][1]
                }
                names = tuple(name for name in names if not open_ids <= walked[name][1])
                ids = tuple(value for value in ids if value in open_ids)
                if not names:
                    return answers
            found = held_many(
                backend=self,
                resource_type=resource_type,
                actions=names,
                actor=subject,
                ids=ids,
                using=alias,
                context=context,
            )
            for name, held_ids in found.items():
                answers[name] = answers.get(name, set()) | held_ids
            return answers

    @_schema_operation
    def grants_all(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Whether the actor holds the action on every object of the type."""
        if not _enforcing():
            return True
        from ..compile.read import grants_all
        from ..models import active_relationship_model

        with _published_for(action):
            return grants_all(
                backend=self,
                resource_type=resource_type,
                action=action,
                actor=subject,
                using=active_relationship_model().objects.db,
                context=context,
            )

    @_schema_operation
    def lookup_subjects(
        self,
        *,
        resource: ObjectRef,
        action: str,
        subject_type: str,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> Iterable[SubjectRef]:
        from ..compile.read import lookup_subjects
        from ..models import active_relationship_model

        self._validate_consistency(consistency, at_zookie)
        with _published_for(action):
            return lookup_subjects(
                backend=self,
                resource=resource,
                action=action,
                subject_type=subject_type,
                using=active_relationship_model().objects.db,
                context=context,
            )

    @_schema_operation
    def write_relationships(self, writes: Iterable[RelationshipTuple]) -> Zookie:
        from django.db import router, transaction

        from ..models import active_relationship_model
        from ..models.relationship import RelationshipRegistry
        from ..models.resource import RebacResource

        RelationshipModel = active_relationship_model()

        rows = list(writes)
        using = router.db_for_write(RelationshipModel)
        max_xid = 0
        with transaction.atomic(using=using):
            schema = self._write_schema(using)
            for tup in rows:
                self._validate_relationship_tuple(tup, schema=schema)
            ids: dict[tuple[str, str], int] = {}
            if RelationshipModel is RelationshipRegistry:
                ids = RebacResource.upsert_refs_bulk(
                    sorted(
                        {(tup.resource.resource_type, tup.resource.resource_id) for tup in rows}
                        | {(tup.subject.subject_type, tup.subject.subject_id) for tup in rows}
                    ),
                    using=using,
                )
            manager = RelationshipModel._base_manager.db_manager(using)
            for tup in rows:
                xid = self._next_xid()
                max_xid = max(max_xid, xid)
                if RelationshipModel is RelationshipRegistry:
                    key = {
                        "resource_fk_id": ids[
                            (tup.resource.resource_type, tup.resource.resource_id)
                        ],
                        "relation": tup.relation,
                        "subject_fk_id": ids[(tup.subject.subject_type, tup.subject.subject_id)],
                        "optional_subject_relation": tup.subject.optional_relation,
                        "caveat_name": tup.caveat_name,
                    }
                else:
                    key = {
                        "resource_type": tup.resource.resource_type,
                        "resource_id": tup.resource.resource_id,
                        "relation": tup.relation,
                        "subject_type": tup.subject.subject_type,
                        "subject_id": tup.subject.subject_id,
                        "optional_subject_relation": tup.subject.optional_relation,
                        "caveat_name": tup.caveat_name,
                    }
                existing = manager.filter(**key).first()
                row = existing if existing is not None else RelationshipModel(**key)
                row.caveat_context = tup.caveat_context or None
                row.expires_at = tup.expires_at
                row.written_at_xid = xid
                # Call Model.save directly so consumer pre/post_save handlers run
                # without opening a savepoint per row.
                models.Model.save(
                    row,
                    using=using,
                    force_insert=existing is None,
                    update_fields=(
                        ["caveat_context", "expires_at", "written_at_xid"]
                        if existing is not None
                        else None
                    ),
                )
        # Zookie token == the maximum xid actually written in the batch,
        # so the token witnesses every row written by this batch. Newer
        # relationship rows remain visible to reads carrying this token.
        # An empty batch returns a token for the current local clock.
        if max_xid == 0:
            return self._zookie()
        mark_relationships_changed()
        return Zookie(self.kind, str(max(max_xid, self._last_xid)))

    @_schema_operation
    def delete_relationships(self, filter_: RelationshipFilter) -> Zookie:
        from django.db import router, transaction

        from ..models import active_relationship_model

        RelationshipModel = active_relationship_model()
        using = router.db_for_write(RelationshipModel)
        with transaction.atomic(using=using):
            schema = self._write_schema(using)
            backed = self._backed_relation_for_filter(
                filter_.resource_type, filter_.resource_id, filter_.relation, schema=schema
            )
            if backed is not None:
                resource_type, relation = backed
                raise self._backed_write_error(resource_type, relation, schema=schema)
            RelationshipModel.objects.using(using).filter(**filter_.lookups()).delete()
        mark_relationships_changed()
        return self._zookie()

    @_schema_operation
    def delete_relationship(self, tuple_: RelationshipTuple) -> Zookie:
        # Local-only convenience verb: exact-match delete for one tuple shape
        # (treats empty optional_subject_relation / caveat_name as exact
        # values, where ``delete_relationships`` treats them as wildcards).
        # Has no direct SpiceDB equivalent; SpiceDB expresses the same intent
        # via ``WriteRelationships`` with ``OPERATION_DELETE``. Planned to
        # lower through that path in 0.4 once the ABC takes operation-shaped
        # updates — see ARCHITECTURE.md.
        from django.db import router, transaction

        from ..models import active_relationship_model

        RelationshipModel = active_relationship_model()
        using = router.db_for_write(RelationshipModel)
        with transaction.atomic(using=using):
            schema = self._write_schema(using)
            definition = schema.get_definition(tuple_.resource.resource_type)
            if definition is not None:
                relation = _find_relation(definition, tuple_.relation)
                if relation is not None and relation.has_backing(tuple_.resource.resource_id):
                    raise self._backed_write_error(
                        tuple_.resource.resource_type, relation, schema=schema
                    )
            RelationshipModel.objects.using(using).filter(
                resource_type=tuple_.resource.resource_type,
                resource_id=tuple_.resource.resource_id,
                relation=tuple_.relation,
                subject_type=tuple_.subject.subject_type,
                subject_id=tuple_.subject.subject_id,
                optional_subject_relation=tuple_.subject.optional_relation,
                caveat_name=tuple_.caveat_name,
            ).delete()
        mark_relationships_changed()
        return self._zookie()

    # ---------- helpers ----------

    def _next_xid(self) -> int:
        self._xid_counter += 1
        self._last_xid = max(int(time.time_ns()) + self._xid_counter, self._last_xid + 1)
        return self._last_xid

    def _zookie(self) -> Zookie:
        return Zookie(self.kind, str(self._next_xid()))

    def _write_schema(self, using: str) -> Schema:
        """The effective policy a relationship write is validated against: the published one.

        ``REBAC_INHERIT_THROUGH_LINKS`` narrows permissions only; relations are the same.
        """
        del using
        return self._published_schema()

    def _validate_relationship_tuple(self, tup: RelationshipTuple, *, schema: Schema) -> None:
        from django.conf import settings
        from django.utils import timezone

        from ..clock import time_max, time_min

        if not tup.resource.resource_id:
            raise ValueError("Resource IDs cannot be empty.")
        if tup.resource.resource_id == "*":
            raise ValueError("'*' is reserved for wildcard subjects, not resource IDs.")
        if not tup.subject.subject_id:
            # A wildcard subject is ``*``; an empty id names no subject.
            raise ValueError("Subject IDs cannot be empty.")
        expires_at = tup.expires_at
        if expires_at is not None:
            if (
                not isinstance(expires_at, datetime)
                or timezone.is_aware(expires_at) != settings.USE_TZ
            ):
                raise ValueError(
                    "Relationship expiration must follow the project's USE_TZ setting."
                )
            if not time_min() < expires_at < time_max():
                raise ValueError(
                    "Relationship expiration must be strictly between TIME_MIN and TIME_MAX."
                )
        definition = schema.get_definition(tup.resource.resource_type)
        if definition is None:
            raise ValueError(f"unknown resource type: {tup.resource.resource_type}")
        relation = _find_relation(definition, tup.relation)
        if relation is None:
            raise ValueError(f"unknown relation: {tup.resource.resource_type}#{tup.relation}")
        if relation.has_backing(tup.resource.resource_id):
            raise self._backed_write_error(tup.resource.resource_type, relation, schema=schema)
        if tup.subject.optional_relation:
            subject_definition = schema.get_definition(tup.subject.subject_type)
            if (
                subject_definition is not None
                and _find_relation(subject_definition, tup.subject.optional_relation) is None
            ):
                raise ValueError(
                    f"subject {tup.subject} does not name a declared relation; "
                    "relationship subjects cannot reference permissions. Migrate the "
                    "schema to a direct object relation and use an arrow to compute "
                    "the target permission"
                )
        if not _subject_allowed_by_relation(relation, tup.subject):
            raise ValueError(
                f"subject {tup.subject} is not allowed for "
                f"{tup.resource.resource_type}#{tup.relation}"
            )
        if not _subject_allowed_by_relation(relation, tup.subject, caveat_name=tup.caveat_name):
            raise ValueError(
                f"caveat {tup.caveat_name!r} is not allowed for subject {tup.subject} on "
                f"{tup.resource.resource_type}#{tup.relation}"
            )
        if tup.caveat_name and schema.get_caveat(tup.caveat_name) is None:
            raise ValueError(f"unknown caveat: {tup.caveat_name}")
        if tup.expires_at is not None and not relation.with_expiration:
            raise ValueError(
                f"expiration is not allowed for {tup.resource.resource_type}#{tup.relation}"
            )

    def _backed_write_error(
        self, resource_type: str, relation: Relation, *, schema: Schema
    ) -> SchemaError:
        if isinstance(relation.backing, ConstBinding):
            return SchemaError(
                f"relation `{relation.name}` on `{resource_type}` is const-backed "
                f"(rebac:const={relation.backing.target_id}); it is synthetic and holds no tuples"
            )
        if isinstance(relation.backing, AttributeBinding):
            return SchemaError(
                f"relation `{relation.name}` on `{resource_type}` is "
                f"attribute-backed; set the subject model's "
                f"`{relation.backing.field}` field instead"
            )
        model_hint = resource_type
        field_hint = (
            relation.backing.path if isinstance(relation.backing, FieldBinding) else relation.name
        )
        definition = schema.get_definition(resource_type)
        if definition is not None:
            field_backing = resolve_field_backing(definition, relation)
            if field_backing is not None:
                model_hint = field_backing.source_model.__name__
                field_hint = (
                    field_backing.generic.name
                    if field_backing.generic is not None
                    else field_backing.field.name
                )
        return SchemaError(
            f"relation `{relation.name}` on `{resource_type}` is field-backed; "
            f"set `{model_hint}.{field_hint}` instead"
        )

    def _backed_relation_for_filter(
        self,
        resource_type: str,
        resource_id: str,
        relation_name: str,
        *,
        schema: Schema,
    ) -> tuple[str, Relation] | None:
        if not relation_name:
            return None
        definitions: list[Definition] = []
        if resource_type:
            definition = schema.get_definition(resource_type)
            if definition is not None:
                definitions.append(definition)
        else:
            definitions.extend(schema.definitions)
        for definition in definitions:
            relation = _find_relation(definition, relation_name)
            if relation is not None and relation.has_backing(resource_id or None):
                return definition.resource_type, relation
        return None


# ---------- Module-level helpers ----------


def mark_relationships_changed() -> None:
    """Invalidate cached permission decisions after relationship rows changed.

    Backend writes call this themselves. Lifecycle code that changes rows
    outside ``write_relationships`` / ``delete_relationships`` (for example
    the ``post_delete`` cascade) calls it so every local backend instance and
    every evaluator scope in this process drops its decisions.
    """
    global _relationship_generation
    with _backend_registry_lock:
        _relationship_generation += 1
    from ..models import active_relationship_model
    from ..snapshot import broken

    # A write inside a read snapshot (SQLite cannot refuse it) ends its standing.
    broken(active_relationship_model().objects.db)
    from django.db import connections, transaction

    written = _uncommitted_transactions()
    for connection in connections.all(initialized_only=True):
        if _manual_transaction(connection):
            written[connection.alias] = _MANUAL_TRANSACTION
        elif connection.in_atomic_block and connection.atomic_blocks:
            outermost = connection.atomic_blocks[0]
            if written.get(connection.alias) is not outermost:
                written[connection.alias] = outermost
                transaction.on_commit(
                    partial(_forget_uncommitted, connection.alias, outermost),
                    using=connection.alias,
                )


def _manual_transaction(connection: Any) -> bool:
    """Whether ``connection`` is in a transaction the application ends itself.

    Autocommit is off and no ``atomic`` block owns the commit: none is open, or
    the outermost was entered with autocommit already off and leaves the
    transaction open on exit.
    """
    if connection.connection is None:
        return False
    if connection.in_atomic_block:
        return not connection.commit_on_exit
    return not connection.get_autocommit()


def _uncommitted_transactions() -> dict[str, object]:
    written: dict[str, object] | None = getattr(_uncommitted_relationships, "written", None)
    if written is None:
        written = _uncommitted_relationships.written = {}
    return written


def _forget_uncommitted(alias: str, transaction: object) -> None:
    """The transaction committed: its rows stay, and every thread may read them now.

    The generation moved when the rows were written. A scope on another thread
    may have read between that write and this commit, so it moves again.
    """
    global _relationship_generation
    written = _uncommitted_transactions()
    if written.get(alias) is transaction:
        del written[alias]
    with _backend_registry_lock:
        _relationship_generation += 1


def uncommitted_relationship_writes(using: str) -> bool:
    """Whether this thread wrote relationship rows in the transaction still open on ``using``.

    The rows may roll back, so nothing read from them is kept until that
    transaction ends: by commit, or when the connection is seen outside it. A
    rolled-back transaction whose outermost ``atomic`` object is entered again
    (a decorated function) still counts as open until the thread is seen
    outside a transaction, and a transaction the application ends itself counts
    as open until autocommit returns; both only cost reads.
    """
    from django.db import connections

    written = _uncommitted_transactions()
    recorded = written.get(using)
    if recorded is None:
        return False
    connection = connections[using]
    if recorded is _MANUAL_TRANSACTION:
        still_open = _manual_transaction(connection)
    else:
        still_open = (
            connection.in_atomic_block
            and bool(connection.atomic_blocks)
            and connection.atomic_blocks[0] is recorded
        )
    if not still_open:
        del written[using]
    return still_open


def mark_db_loaded_schemas_stale(**kwargs: Any) -> None:
    """Invalidate all live LocalBackend instances backed by Schema* DB rows."""
    with _backend_registry_lock:
        backends = list(_db_loaded_backends)
    for backend in backends:
        backend.mark_schema_stale()


# Settings affect backing identities and interval sentinels too. Reuse the
# backend's invalidation registry, including explicit/manual backend facts.
setting_changed.connect(mark_db_loaded_schemas_stale)
