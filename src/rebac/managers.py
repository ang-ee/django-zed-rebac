"""RebacManager + RebacQuerySet.

Per ARCHITECTURE.md § Three actor-resolution paths:
  1. Per-queryset actor/action (.with_actor / .with_action)
  2. Per-queryset sudo (.sudo) — bypass with a mandatory reason
  3. current_actor() ContextVar — populated by middleware / Celery hooks
  4. Fallback — STRICT_MODE=True raises; else system_context()

The actor lives on the queryset instance (NOT a ContextVar). It survives
chaining via `_clone()` and propagates into instances via `from_db()`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any, TypeVar

from asgiref.sync import sync_to_async
from django.db import models

from ._id import resource_id_attr
from .actors import current_actor as _current_actor
from .actors import grant_subject_ref, to_subject_ref
from .actors import is_sudo as _is_sudo_ambient
from .conf import app_settings
from .errors import MissingActorError, PermissionDenied
from .field_visibility import (
    accessible_ids,
    apply_field_visibility,
    backend_grants_all,
    effective_field_deny_mode,
    gated_read_fields,
    projection_field_names,
    runtime_field_deny_mode,
    validate_field_deny_mode,
    warn_raise_mode_degrades,
)
from .resources import model_resource_type
from .types import FieldDenyMode, SubjectRef

_M = TypeVar("_M", bound=models.Model)


class RebacQuerySet(models.QuerySet[_M]):
    """REBAC-aware queryset.

    Use `.with_actor(actor)`, `.as_user(user)`, `.as_agent(agent, on_behalf_of=u)`,
    or `.sudo(reason=...)` to scope. Materialising without any of those AND
    without an ambient actor raises `MissingActorError` when STRICT_MODE is on.

    Generic over the model so the actor verbs preserve the concrete row
    type: ``Post.objects.filter(...).as_user(u).get()`` stays ``Post``.
    """

    # Per-queryset state. Carried through `_clone()`.
    _rebac_actor: SubjectRef | None
    _rebac_action: str | None
    _rebac_sudo_reason: str | None
    _rebac_field_deny: FieldDenyMode | None
    _rebac_select_related_guards: tuple[str, ...]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rebac_actor = None
        self._rebac_action = None
        self._rebac_sudo_reason = None
        self._rebac_field_deny = None
        self._rebac_select_related_guards = ()

    # Note: a second `_clone` override below combines the actor + scope-flag
    # propagation; this stub kept for readability.

    # ----- Actor verbs -----

    def with_actor(self, actor: Any) -> RebacQuerySet[_M]:
        """Pin a SubjectRef on the queryset. Generic verb — accepts any ActorLike."""
        ref = actor if isinstance(actor, SubjectRef) else to_subject_ref(actor)
        clone = self._clone()
        clone._rebac_actor = ref
        clone._rebac_sudo_reason = None
        return clone

    def as_user(self, user: Any) -> RebacQuerySet[_M]:
        """Typed shorthand: scope to a Django User."""
        return self.with_actor(to_subject_ref(user))

    def as_agent(self, agent: Any, *, on_behalf_of: Any | None = None) -> RebacQuerySet[_M]:
        """Typed shorthand: scope to an agent acting via a Grant."""
        return self.with_actor(grant_subject_ref(agent, on_behalf_of))

    def with_action(self, action: str) -> RebacQuerySet[_M]:
        """Pin the REBAC permission used for queryset read scoping."""
        if not action:
            raise ValueError("with_action() requires a non-empty action")
        clone = self._clone()
        clone._rebac_action = action
        clone._rebac_scope_applied = False
        return clone

    def on_field_deny(self, mode: FieldDenyMode) -> RebacQuerySet[_M]:
        """Override ``REBAC_FIELD_READ_MODE`` for this queryset."""
        if mode == "raise":
            warn_raise_mode_degrades(stacklevel=2)
        clone = self._clone()
        clone._rebac_field_deny = validate_field_deny_mode(mode)
        return clone

    def for_write(self) -> RebacQuerySet[_M]:
        """Return a write-target queryset: REBAC row scope kept, field-read
        redaction off.

        Resolving an update/delete target must load every column of the row to
        mutate it, so field-read redaction must not hide columns from the loaded
        instance -- yet the row must still be one the actor may access (row scope
        is applied on materialization as usual). ``for_write()`` is
        ``on_field_deny("allow")`` named for that intent.
        """
        return self.on_field_deny("allow")

    def sudo(self, *, reason: str) -> RebacQuerySet[_M]:
        """Bypass REBAC for this queryset. Mandatory `reason`."""
        return self._bypass(reason=reason, allow_when_sudo_disabled=False)

    def system_context(self, *, reason: str) -> RebacQuerySet[_M]:
        return self._bypass(reason=reason, allow_when_sudo_disabled=True)

    def _bypass(self, *, reason: str, allow_when_sudo_disabled: bool) -> RebacQuerySet[_M]:
        from .errors import SudoNotAllowedError, SudoReasonRequiredError

        if not allow_when_sudo_disabled and not app_settings.REBAC_ALLOW_SUDO:
            raise SudoNotAllowedError("sudo() denied: REBAC_ALLOW_SUDO is False")
        if app_settings.REBAC_REQUIRE_SUDO_REASON and not reason:
            raise SudoReasonRequiredError(
                "sudo() requires reason= when REBAC_REQUIRE_SUDO_REASON=True"
            )
        clone = self._clone()
        clone._rebac_actor = None
        clone._rebac_sudo_reason = reason
        return clone

    def actor(self) -> SubjectRef | None:
        return self._rebac_actor

    def is_sudo(self) -> bool:
        return self._rebac_sudo_reason is not None

    # ----- Relation loading -----

    def rebac_select_related(self, *fields: Any) -> RebacQuerySet[_M]:
        """``select_related`` plus a post-fetch guard for REBAC-bound targets."""
        clone: RebacQuerySet[_M] = self.select_related(*fields)
        if fields == (None,):
            clone._rebac_select_related_guards = ()
            return clone
        from .relation_loading import selected_related_paths

        paths = (
            tuple(str(field) for field in fields)
            if fields
            else selected_related_paths(
                self.model,
                clone.query.select_related,
            )
        )
        clone._rebac_select_related_guards = tuple(
            dict.fromkeys((*self._rebac_select_related_guards, *paths))
        )
        return clone

    def rebac_prefetch_related(self, *lookups: Any) -> RebacQuerySet[_M]:
        """``prefetch_related`` with actor-scoped querysets for protected targets."""
        if lookups == (None,):
            return self.prefetch_related(None)
        from .relation_loading import relation_actor, scope_prefetch_lookups

        scoped = scope_prefetch_lookups(
            self.model,
            lookups,
            actor=relation_actor(self),
            mode=self._rebac_field_deny,
        )
        return self.prefetch_related(*scoped)

    # ----- Materialisation -----

    def _resolve_effective_actor(self) -> tuple[SubjectRef | None, bool]:
        """Returns (actor_ref, is_sudo).

        Resolution order: per-queryset → ambient ContextVar → strict-mode fallback.
        """
        if self._rebac_sudo_reason is not None:
            return (None, True)
        if self._rebac_actor is not None:
            return (self._rebac_actor, False)
        if _is_sudo_ambient():
            return (None, True)
        ambient = _current_actor()
        if ambient is not None:
            return (ambient, False)
        if app_settings.REBAC_STRICT_MODE:
            raise MissingActorError(
                f"Queryset on {self.model.__name__} materialised without an actor. "
                f"Use .with_actor(actor), .as_user(user), .as_agent(agent, on_behalf_of=user), "
                f"or .sudo(reason='...'). Or set REBAC_STRICT_MODE=False (NOT recommended)."
            )
        # STRICT_MODE=False: fall through unscoped.
        return (None, True)

    _rebac_scope_applied: bool = False

    def _apply_scope_in_place(self) -> None:
        """Inject ``<id_attr>__in=<accessible>`` onto the query's WHERE clause.

        ``id_attr`` defaults to ``"pk"`` but can be flipped per-model via
        ``Meta.rebac_id_attr`` or globally via ``REBAC_RESOURCE_ID_ATTR``.

        Avoids ``self.filter(...)`` because ``.get()`` pre-slices the
        queryset and ``.filter()`` rejects post-slice. ``Q.add_q``
        operates at the SQL level and bypasses the slice check.

        ``backend().accessible()`` is memoised per-actor + action +
        resource_type via the ambient ``_accessible_cache`` ContextVar
        (``rebac.actors``). A single GraphQL request that materialises
        the same scoped queryset multiple times (aggregate primary
        + ``totalCount`` + per-measure resolvers, paginated lookups,
        nested edges) collapses to one ``accessible()`` graph walk —
        the underlying relationship SQL is bounded by the schema's
        depth, not by the number of resolvers fired.
        """
        if self._rebac_scope_applied:
            return
        actor, sudo = self._resolve_effective_actor()
        self._rebac_scope_applied = True
        if sudo:
            return
        # ``_resolve_effective_actor`` only returns ``sudo=False`` paired
        # with a non-None actor (the None cases all carry ``sudo=True``).
        assert actor is not None
        rebac_type = model_resource_type(self.model)
        if not rebac_type:
            return
        from django.db.models import Q

        from .backends import backend

        action = str(
            self._rebac_action or getattr(self.model._meta, "rebac_default_action", "read")
        )
        active_backend = backend()
        if backend_grants_all(
            active_backend,
            subject=actor,
            action=action,
            resource_type=rebac_type,
        ):
            return
        ids: list[Any] = list(
            accessible_ids(
                active_backend,
                subject=actor,
                action=action,
                resource_type=rebac_type,
            )
        )
        attr = resource_id_attr(self.model)
        if attr == "pk":
            # Coerce to ints when the PK is integer-typed; leave as
            # strings for UUID/Char PKs. Only relevant for the pk path
            # — non-pk attrs (sqid, public_id, slug) are always
            # string-valued and pass through unchanged.
            try:
                pk_field = self.model._meta.pk
                if pk_field is not None and pk_field.get_internal_type() in (
                    "AutoField",
                    "BigAutoField",
                    "IntegerField",
                    "BigIntegerField",
                    "SmallIntegerField",
                    "PositiveIntegerField",
                    "PositiveBigIntegerField",
                    "PositiveSmallIntegerField",
                ):
                    ids = [int(i) for i in ids]
            except ValueError:
                pass
            except TypeError:
                pass
        # ``Q.add_q`` works even on sliced queries.
        self.query.add_q(Q(**{f"{attr}__in": ids}))

    def _clone(self, **kwargs: Any) -> RebacQuerySet[_M]:
        # ``QuerySet._clone`` is a real method django-stubs intentionally
        # omits from the public stub surface.
        clone: RebacQuerySet[_M] = super()._clone(**kwargs)  # type: ignore[misc]
        clone._rebac_actor = self._rebac_actor
        clone._rebac_action = self._rebac_action
        clone._rebac_sudo_reason = self._rebac_sudo_reason
        clone._rebac_field_deny = self._rebac_field_deny
        clone._rebac_select_related_guards = self._rebac_select_related_guards
        # Important: each clone re-applies scope when needed.
        clone._rebac_scope_applied = False
        return clone

    def _effective_field_mode(self) -> FieldDenyMode:
        return effective_field_deny_mode(self._rebac_field_deny)

    def _guard_projected_field_reads(self, actor: SubjectRef | None, sudo: bool) -> None:
        if actor is None or sudo:
            return
        self._guard_projected_related_reads()
        if runtime_field_deny_mode(self._effective_field_mode()) == "allow":
            return
        projected = projection_field_names(self.model, getattr(self, "_fields", None))
        if projected is None:
            return
        gated = gated_read_fields(self.model)
        if not gated:
            return
        requested = gated if not projected else gated & projected
        if requested:
            names = ", ".join(f"read__{name}" for name in sorted(requested))
            raise PermissionDenied(
                f"Cannot project gated field(s) {names} on {self.model.__name__}: "
                "field read enforcement requires model-instance materialisation "
                "or a projection that omits gated fields."
            )

    def _guard_projected_related_reads(self) -> None:
        if not self._rebac_select_related_guards:
            return
        raw_fields = getattr(self, "_fields", None)
        if raw_fields is None:
            return
        guarded = tuple(f"{path}__" for path in self._rebac_select_related_guards)
        related = sorted(
            field
            for field in raw_fields
            if isinstance(field, str) and any(field.startswith(prefix) for prefix in guarded)
        )
        if not related:
            return
        names = ", ".join(related[:5])
        raise PermissionDenied(
            f"Cannot project related field(s) {names}: rebac_select_related() "
            "enforcement requires model-instance materialisation."
        )

    def _fetch_all(self) -> None:
        if self._result_cache is None:
            self._apply_scope_in_place()
        actor, sudo = self._resolve_effective_actor()
        self._guard_projected_field_reads(actor, sudo)
        super()._fetch_all()
        if self._result_cache is not None:
            if actor is not None and not sudo:
                for inst in self._result_cache:
                    if isinstance(inst, models.Model):
                        inst._rebac_actor = actor  # type: ignore[attr-defined]
                        inst._rebac_field_deny = self._rebac_field_deny  # type: ignore[attr-defined]
                apply_field_visibility(
                    self._result_cache,
                    model=self.model,
                    actor=actor,
                    mode=self._effective_field_mode(),
                )
            self._guard_selected_related_reads()

    def iterator(self, *args: Any, **kwargs: Any) -> Any:
        if self._result_cache is None:
            self._apply_scope_in_place()
        actor, sudo = self._resolve_effective_actor()
        self._guard_projected_field_reads(actor, sudo)
        rows = list(super().iterator(*args, **kwargs))
        if actor is not None and not sudo:
            for inst in rows:
                if isinstance(inst, models.Model):
                    inst._rebac_actor = actor  # type: ignore[attr-defined]
                    inst._rebac_field_deny = self._rebac_field_deny  # type: ignore[attr-defined]
            apply_field_visibility(
                rows,
                model=self.model,
                actor=actor,
                mode=self._effective_field_mode(),
            )
        self._guard_selected_related_reads(rows)
        return iter(rows)

    def _guard_selected_related_reads(self, rows: Iterable[Any] | None = None) -> None:
        if not self._rebac_select_related_guards:
            return
        from .relation_loading import guard_selected_related_instances, relation_actor

        if rows is None:
            rows = self._result_cache or ()
        guard_selected_related_instances(
            rows,
            root_model=self.model,
            paths=self._rebac_select_related_guards,
            actor=relation_actor(self),
            mode=self._effective_field_mode(),
        )

    # ----- Async iteration: Django's aiterator bypasses the sync overrides -----

    async def aiterator(self, *args: Any, **kwargs: Any) -> AsyncIterator[_M]:
        """Async variant of :meth:`iterator` that preserves REBAC scoping.

        Django's ``QuerySet.aiterator`` builds the row iterable directly rather
        than routing through the (overridden) sync ``iterator`` / ``_fetch_all``
        path, so without this override an ``async for`` over ``aiterator()``
        would bypass actor scoping entirely — an unscoped read. We apply the
        same scope filter, actor stamping, field-visibility redaction, and
        ``rebac_select_related`` guards as the sync path, running the
        DB-touching steps off the event loop via ``sync_to_async`` (same
        posture as the rest of the async surface, which Django itself wraps in
        ``sync_to_async`` around our sync overrides).

        Every other async ORM method (``aget`` / ``acount`` / ``aexists`` /
        ``afirst`` / ``aupdate`` / ``adelete`` / ``acreate`` / ``__aiter__`` /
        ``ain_bulk`` / …) is a ``sync_to_async`` wrapper around the sync method
        we already override, so it inherits scoping for free — only
        ``aiterator`` and :meth:`aggregate` need explicit handling.
        """
        if self._result_cache is None:
            await sync_to_async(self._apply_scope_in_place)()
        actor, sudo = self._resolve_effective_actor()
        self._guard_projected_field_reads(actor, sudo)
        field_mode = self._effective_field_mode()
        redact = runtime_field_deny_mode(field_mode) != "allow" and bool(
            gated_read_fields(self.model)
        )
        guard_related = bool(self._rebac_select_related_guards)
        async for inst in super().aiterator(*args, **kwargs):
            if actor is not None and not sudo and isinstance(inst, models.Model):
                inst._rebac_actor = actor  # type: ignore[attr-defined]
                inst._rebac_field_deny = self._rebac_field_deny  # type: ignore[attr-defined]
                if redact:
                    await sync_to_async(apply_field_visibility)(
                        [inst], model=self.model, actor=actor, mode=field_mode
                    )
                if guard_related:
                    await sync_to_async(self._guard_selected_related_reads)([inst])
            yield inst

    # ----- Counts / existence respect scope too -----

    def count(self) -> int:
        if self._result_cache is not None:
            return len(self._result_cache)
        self._apply_scope_in_place()
        return super().count()

    def exists(self) -> bool:
        if self._result_cache is not None:
            return bool(self._result_cache)
        self._apply_scope_in_place()
        return super().exists()

    def aggregate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Aggregate over the actor's scope, not the whole table.

        Django computes ``aggregate`` against ``self.query`` directly, so it
        never triggers ``_fetch_all`` / ``iterator``. Without scoping the WHERE
        clause first, ``SomeModel.objects.as_user(u).aggregate(Count("pk"))``
        would summarise rows ``u`` cannot read (and ``aaggregate`` — a
        ``sync_to_async`` wrapper around this method — would leak the same way).
        Applying scope here fixes both surfaces.
        """
        self._apply_scope_in_place()
        return super().aggregate(*args, **kwargs)

    # ----- Write ops: enforce all-or-nothing -----

    def update(self, **kwargs: Any) -> int:
        actor, sudo = self._resolve_effective_actor()
        if sudo:
            return super().update(**kwargs)
        rebac_type = model_resource_type(self.model)
        if rebac_type:
            self._guard_bulk_action(actor, "write")  # type: ignore[arg-type]
            # Per-field write gates — same all-or-nothing semantics as the
            # resource-level write check above. For each kwarg whose field
            # has a ``write__<f>`` permission declared, every affected row
            # must also pass that check.
            self._guard_bulk_field_writes(actor, kwargs)  # type: ignore[arg-type]
        return super().update(**kwargs)

    def delete(self) -> tuple[int, dict[str, int]]:
        actor, sudo = self._resolve_effective_actor()
        if sudo:
            return super().delete()
        rebac_type = model_resource_type(self.model)
        if rebac_type:
            self._guard_bulk_action(actor, "delete")  # type: ignore[arg-type]
        return super().delete()

    def _guard_bulk_action(self, actor: SubjectRef, action: str) -> None:
        from .backends import backend

        rebac_type = model_resource_type(self.model)
        if not rebac_type:
            return
        # Pre-fetch ids in scope, intersect with allowed.
        affected = self._affected_resource_ids_for_guard()
        if not affected:
            return
        allowed = set(backend().accessible(subject=actor, action=action, resource_type=rebac_type))
        denied = affected - allowed
        if denied:
            sample = ", ".join(sorted(denied)[:5])
            raise PermissionDenied(
                f"Bulk {action}: {len(denied)} row(s) outside actor scope (e.g. {sample}). "
                f"Bulk operations are all-or-nothing."
            )

    def _guard_bulk_field_writes(self, actor: SubjectRef, kwargs: dict[str, Any]) -> None:
        """Per-field write enforcement for bulk ``QuerySet.update()``.

        Mirrors the per-field gate run in ``signals.pre_save``: for each
        ``f`` in ``kwargs`` whose resource type declares a permission
        named ``write__<f>``, every row in the current scope must pass
        that check too — same all-or-nothing semantics as the
        resource-level check. Any denied row aborts the whole update.

        Backends without an in-process schema accessor (i.e. SpiceDB
        once 0.5 lands) skip this — they'll route per-field checks
        through their own server-side schema. The resource-level
        ``write`` check already ran before we got here.
        """
        from .backends import backend
        from .schema.ast import Schema
        from .schema.walker import field_gated_actions

        rebac_type = model_resource_type(self.model)
        if not rebac_type:
            return
        accessor = getattr(backend(), "schema", None)
        if not callable(accessor):
            return
        try:
            schema = accessor()
        except Exception:
            return
        if not isinstance(schema, Schema):
            return
        definition = schema.get_definition(rebac_type)
        if definition is None:
            return
        declared = field_gated_actions(definition, "write")
        if not declared:
            return

        affected = self._affected_resource_ids_for_guard()
        if not affected:
            return

        meta = self.model._meta
        for raw_name in kwargs.keys():
            # Normalise FK attname → field.name so ``write__folder`` matches
            # an ``update(folder_id=...)`` call.
            try:
                field = meta.get_field(raw_name)
                field_name = field.name
            except Exception:
                field_name = raw_name
            action = f"write__{field_name}"
            if action not in declared:
                continue
            allowed = set(
                accessible_ids(
                    backend(),
                    subject=actor,
                    action=action,
                    resource_type=rebac_type,
                )
            )
            denied = affected - allowed
            if denied:
                sample = ", ".join(sorted(denied)[:5])
                raise PermissionDenied(
                    f"Bulk {action}: {len(denied)} row(s) outside actor scope "
                    f"(e.g. {sample}). Bulk operations are all-or-nothing."
                )

    def _affected_resource_ids_for_guard(self) -> set[str]:
        """Scan the queryset's target rows without read-scoping the guard itself."""
        attr = resource_id_attr(self.model)
        scan = self.system_context(reason="rebac.bulk-guard")
        return {str(v) for v in scan.values_list(attr, flat=True)}


class RebacManager(models.Manager.from_queryset(RebacQuerySet)):  # type: ignore[misc]
    """Manager backed by `RebacQuerySet`.

    Built via ``from_queryset`` so the custom queryset methods (and any
    subclass supplied through ``RebacManager.from_queryset(...)``) are
    copied onto the manager. The actor verbs are re-declared with
    explicit signatures for IDE/type discoverability; the return type is
    ``RebacQuerySet[Any]`` because the ``from_queryset`` base erases the
    model parameter — call ``.with_actor(...)`` on a model-typed
    queryset (e.g. ``Post.objects.all().as_user(u)``) when you need the
    concrete row type preserved through to ``.get()``.
    """

    def get_queryset(self) -> RebacQuerySet[Any]:
        # `_queryset_class` / `_hints` are BaseManager internals; the dynamic
        # `from_queryset` base erases them from pyright's view (mypy sees them
        # via django-stubs). We know the queryset class is RebacQuerySet.
        qs: RebacQuerySet[Any] = self._queryset_class(  # pyright: ignore[reportAttributeAccessIssue]
            model=self.model,
            using=self._db,
            hints=self._hints,  # pyright: ignore[reportAttributeAccessIssue]
        )
        return qs

    def with_actor(self, actor: Any) -> RebacQuerySet[Any]:
        return self.get_queryset().with_actor(actor)

    def as_user(self, user: Any) -> RebacQuerySet[Any]:
        return self.get_queryset().as_user(user)

    def as_agent(self, agent: Any, *, on_behalf_of: Any | None = None) -> RebacQuerySet[Any]:
        return self.get_queryset().as_agent(agent, on_behalf_of=on_behalf_of)

    def with_action(self, action: str) -> RebacQuerySet[Any]:
        return self.get_queryset().with_action(action)

    def on_field_deny(self, mode: FieldDenyMode) -> RebacQuerySet[Any]:
        return self.get_queryset().on_field_deny(mode)

    def for_write(self) -> RebacQuerySet[Any]:
        return self.get_queryset().for_write()

    def rebac_select_related(self, *fields: Any) -> RebacQuerySet[Any]:
        return self.get_queryset().rebac_select_related(*fields)

    def rebac_prefetch_related(self, *lookups: Any) -> RebacQuerySet[Any]:
        return self.get_queryset().rebac_prefetch_related(*lookups)

    def sudo(self, *, reason: str) -> RebacQuerySet[Any]:
        return self.get_queryset().sudo(reason=reason)

    def system_context(self, *, reason: str) -> RebacQuerySet[Any]:
        return self.get_queryset().system_context(reason=reason)
