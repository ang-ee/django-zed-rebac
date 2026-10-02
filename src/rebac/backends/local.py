"""LocalBackend — permissions compiled to queries, and transactional tuple writes.

Proposed-object evaluation lives in preflight and the shared schema walker.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any
from weakref import WeakSet

from django.core.signals import setting_changed
from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper

from ..errors import SchemaError
from ..field_backing import (
    resolve_field_backing,
)
from ..resources import model_resource_type
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
from .base import Backend

_backend_registry_lock = Lock()
_db_loaded_backends: WeakSet[LocalBackend] = WeakSet()


@dataclass
class _SchemaFacts:
    """Whole-schema facts memoised per schema generation (see ``_schema_facts``)."""

    generation: int
    live_types: frozenset[str] | None = None


_relationship_generation = 0


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
    """Return contracts the local runtime must reject before evaluation."""
    from ..schema.parser import subject_relation_errors, validate_schema

    backing_errors = [
        error
        for error in validate_schema(schema)
        if "backed relation" in error or "backing" in error
    ]
    return backing_errors + subject_relation_errors(schema)


class LocalBackend(Backend):
    """Evaluates permissions as queries over the application's own tables."""

    kind = "local"

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
        self._schema_facts_memo: dict[int, _SchemaFacts] = {}
        # Counter used as a stable monotonic xid on backends (e.g. SQLite test
        # mode) without `txid_current()`.
        self._xid_counter = 0
        self._last_xid = 0
        with _backend_registry_lock:
            _db_loaded_backends.add(self)

    # ---------- Schema management ----------

    def set_schema(self, schema: Schema) -> None:
        """Install the in-memory schema. Called by the sync command."""
        schema_errors = _enforced_schema_errors(schema)
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
            self._schema_generation += 1

    def schema(self) -> Schema:
        return self._schema_snapshot().schema

    def _manual_schema_revision(self) -> str:
        """The digest of the manual schema; set_schema performs no I/O."""
        with self._schema_lock:
            return self._manual_revision

    def _schema_snapshot(self, known_revision: str | None = None) -> SchemaSnapshot:
        if known_revision is None:
            return self._load_schema_snapshot()
        return self._load_schema_snapshot(known_revision)

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
        # Manual transactions have no observable on_commit lifecycle. Keep
        # their pins operation-local, as before revision caching was added.
        scoped = connection.get_autocommit() or (
            connection.in_atomic_block and connection.commit_on_exit
        )
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
                self._schema_facts_memo.pop(snapshot.generation, None)

    def _read_schema_revision(self, connection: BaseDatabaseWrapper) -> str | None:
        """The published policy revision on the connection's alias."""
        from ..models.generation import SchemaGeneration

        return SchemaGeneration.objects.revision(connection.alias)

    def _cache_generation(self, resource_type: str) -> tuple[Any, ...] | None:
        """Return a decision generation for ``resource_type``, or ``None`` to bypass caching.

        Decisions are not cached inside a transaction (a rollback would leave
        stale answers), when relationships can expire without a write, or when
        the resource type can reach live ORM backing. Expired schema snapshots
        refresh before a cache key is reused; warm scope hits perform no SQL.
        """
        from django.db import connections

        from ..models import SchemaDefinition, active_relationship_model

        candidates = (
            connections[active_relationship_model().objects.db],
            connections[SchemaDefinition.objects.db],
        )
        for candidate in candidates:
            if candidate.in_atomic_block or (
                candidate.connection is not None and not candidate.get_autocommit()
            ):
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
        return snapshot.generation, _relationship_generation

    def _schema_facts(self, snapshot: SchemaSnapshot) -> _SchemaFacts:
        """Whole-schema facts derived once per schema generation."""
        from ..schema.introspection import live_backed_resource_types

        with self._schema_lock:
            cached = self._schema_facts_memo.get(snapshot.generation)
            if cached is not None and cached.live_types is not None:
                return cached
            # Public introspection keeps its 0.18.2 shape. Filtered constants
            # are a retained 0.19 live source and must also bypass decisions.
            live = set(live_backed_resource_types(snapshot.schema))
            live.update(
                definition.resource_type
                for definition in snapshot.schema.definitions
                if any(
                    isinstance(relation.backing, ConstBinding) and relation.backing.filters
                    for relation in definition.relations
                )
            )
            while True:
                expanded = live | {
                    definition.resource_type
                    for definition in snapshot.schema.definitions
                    if any(
                        allowed.type in live
                        for relation in definition.relations
                        for allowed in relation.allowed_subjects
                    )
                }
                if expanded == live:
                    break
                live = expanded
            facts = cached if cached is not None else _SchemaFacts(snapshot.generation)
            facts.live_types = frozenset(live)
            current = (snapshot.generation == self._schema_generation) or any(
                s.generation == snapshot.generation for s in self._schema_snapshots.values()
            )
            if snapshot.generation != -1 and current:
                self._schema_facts_memo[snapshot.generation] = facts
            return facts

    def mark_schema_stale(self) -> None:
        """Discard compiled facts and invalidate DB-loaded schema snapshots."""
        # Loaders perform no database I/O under this lock, so a signal holding
        # database write locks can safely evict shared snapshots here.
        with self._schema_lock, self._schema_invalidation_lock:
            self._schema_facts_memo.clear()
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
        )

    def check_bulk_permissions(
        self,
        items: Iterable[CheckItem],
        *,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> list[CheckResult]:
        self._validate_consistency(consistency, at_zookie)
        from ..compile.read import check_many
        from ..models import active_relationship_model

        return check_many(
            backend=self, items=list(items), using=active_relationship_model().objects.db
        )

    @_schema_operation
    def queryset_filter(
        self,
        *,
        model: type[models.Model],
        subject: SubjectRef,
        action: str,
        using: str,
    ) -> models.Q | None:
        from ..compile.read import scope_q

        if model_resource_type(model) is None:
            return None
        return scope_q(backend=self, model=model, action=action, actor=subject, using=using)

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

        self._validate_consistency(consistency, at_zookie)
        return list(
            accessible_ids(
                backend=self,
                resource_type=resource_type,
                action=action,
                actor=subject,
                using=active_relationship_model().objects.db,
                context=context,
            )
        )

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
        from ..compile.read import grants_all
        from ..models import active_relationship_model

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
        """The effective policy a relationship write is validated against."""
        del using
        return self._schema_snapshot().schema

    def _validate_relationship_tuple(self, tup: RelationshipTuple, *, schema: Schema) -> None:
        from django.conf import settings
        from django.utils import timezone

        from ..clock import time_max, time_min

        if not tup.resource.resource_id:
            raise ValueError("Resource IDs cannot be empty.")
        if tup.resource.resource_id == "*":
            raise ValueError("'*' is reserved for wildcard subjects, not resource IDs.")
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
                field_hint = field_backing.field.name
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


def mark_db_loaded_schemas_stale(**kwargs: Any) -> None:
    """Invalidate all live LocalBackend instances backed by Schema* DB rows."""
    with _backend_registry_lock:
        backends = list(_db_loaded_backends)
    for backend in backends:
        backend.mark_schema_stale()


# Settings affect backing identities and interval sentinels too. Reuse the
# backend's invalidation registry, including explicit/manual backend facts.
setting_changed.connect(mark_db_loaded_schemas_stale)
