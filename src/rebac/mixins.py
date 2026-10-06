"""RebacMixin — model-layer enforcement entry point.

`Meta.rebac_resource_type` is recognised via a custom metaclass that pops the
attribute before delegating to Django's `ModelBase` (which would otherwise
reject it as an unknown Meta option). The value is stored as
`<Model>._meta.rebac_resource_type` after class creation, so callers continue
to read it as a Meta attribute even though Django itself doesn't track it.

Non-model classes (views, menus) use `RebacObjectMeta` instead, which stores
the same keys directly on the class as ``_rebac_resource_type`` etc. rather
than on ``_meta``. ``RebacModelBase`` inherits ``RebacObjectMeta`` and only
overrides where the captured values land.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from django.core.exceptions import ImproperlyConfigured
from django.db import models, router
from django.db.models.base import ModelBase
from django.db.models.options import DEFAULT_NAMES

from ._id import resource_id_attr
from .conf import app_settings
from .errors import PermissionDenied
from .field_visibility import backend_schema
from .managers import RebacManager, TrackedManager, TrackedQuerySet
from .preflight import _check_new_model
from .resources import model_resource_type
from .schema.walker import field_gated_actions
from .types import CheckResult, Consistency, FieldDenyMode, ObjectRef, SubjectRef

REBAC_META_OPTIONS = (
    "rebac_resource_type",
    "rebac_default_action",
    "rebac_subject_relation",
    # Per-model override for the attribute the engine reads when
    # building a resource_id (signals + manager) or a subject_id
    # (``to_subject_ref`` for User / Group). Default resolution order
    # is `Meta.rebac_id_attr` → `app_settings.REBAC_RESOURCE_ID_ATTR`
    # → ``"pk"``. See `_id.resource_id_attr`.
    "rebac_id_attr",
)


def _capture_rebac_meta(attrs: dict[str, Any]) -> dict[str, Any]:
    """Pop recognised REBAC keys off ``class Meta:`` and return them.

    Called before ``super().__new__()`` so neither Django's ``ModelBase``
    nor plain ``type`` ever sees the keys.

    Only keys defined directly on this ``Meta`` (i.e. in ``vars(meta)``) are
    deleted; inherited keys are captured by value but left intact on the
    ancestor class. Otherwise a subclass that reuses a parent's ``Meta``
    (legitimate for ``RebacObjectMeta`` views/menus) would mutate the
    ancestor in place — its second instantiation would silently lose the
    attributes the metaclass relies on.
    """
    meta = attrs.get("Meta")
    captured: dict[str, Any] = {}
    if meta is None:
        return captured
    own = vars(meta)
    for key in REBAC_META_OPTIONS:
        if key in own:
            captured[key] = own[key]
            delattr(meta, key)
        elif hasattr(meta, key):
            captured[key] = getattr(meta, key)
    return captured


INJECTED_BASE_MANAGER = "_rebac_base"


def _declared_base_manager(meta: Any, bases: tuple[type, ...]) -> str | None:
    """The base manager a model declares, in its own ``Meta`` or through a parent model.

    Django's fallback (``Options.base_manager``) reads only the first parent
    model. Every parent is read here, in order, so listing a library mixin
    first cannot discard a base manager declared by a later parent. The
    injected manager and Django's automatic one are not declarations.
    """
    name = getattr(meta, "base_manager_name", None)
    if name is not None:
        return None if name == INJECTED_BASE_MANAGER else str(name)
    for base in bases:
        if hasattr(base, "_meta"):
            inherited = cast(Any, base)._base_manager.name
            if inherited not in ("_base_manager", INJECTED_BASE_MANAGER):
                return str(inherited)
    return None


class RebacObjectMeta(type):
    """Registration metaclass for non-model REBAC resources (views, menus, etc.).

    Captures the same :data:`REBAC_META_OPTIONS` keys as ``RebacModelBase`` but
    stores them directly on the class as ``_rebac_<key>`` attributes rather
    than on ``._meta`` (which only exists on Django models).

    Usage::

        class FileListView(ListView):
            class Meta:
                rebac_resource_type = "app/view"
                rebac_id_attr = "_view_meta.source.operation"

        # After class creation:
        # FileListView._rebac_resource_type == "app/view"
    """

    # Declared so static analysis knows classes built with this metaclass
    # carry these attributes (injected by ``_store_rebac_meta`` at class
    # creation). ``_rebac_resource_type`` is always set (``None`` when no
    # ``Meta.rebac_resource_type`` was given); the other two only appear
    # when their Meta keys were supplied, hence the ``ClassVar`` typing
    # mirrors the runtime "attribute may be absent" contract loosely.
    _rebac_resource_type: str | None
    _rebac_id_attr: str
    _rebac_default_action: str
    # Captured for parity with the model metaclass; only Django models act as
    # subjects, so ``to_subject_ref`` never reads it here (proposal 0006).
    _rebac_subject_relation: str

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        attrs: dict[str, Any],
        **kwargs: Any,
    ) -> type:
        captured = _capture_rebac_meta(attrs)
        mcs._carry_rebac_meta(attrs, captured)
        new_cls = super().__new__(mcs, name, bases, attrs, **kwargs)
        mcs._store_rebac_meta(new_cls, captured)
        return new_cls

    @staticmethod
    def _carry_rebac_meta(attrs: dict[str, Any], captured: dict[str, Any]) -> None:
        """Hand the captured options to the class while it is being built."""

    @staticmethod
    def _store_rebac_meta(target_cls: type, captured: dict[str, Any]) -> None:
        # Keys are already named "rebac_*", so prefix with "_" only:
        # "rebac_resource_type" → "_rebac_resource_type"
        for key, value in captured.items():
            setattr(target_cls, f"_{key}", value)


class RebacModelBase(RebacObjectMeta, ModelBase):
    """Custom metaclass that strips ZED-specific Meta attrs before Django sees them.

    Inherits ``RebacObjectMeta`` for the capture logic and overrides
    ``_carry_rebac_meta`` to stash values onto ``._meta`` so callers can still
    read them as ``<Model>._meta.rebac_resource_type`` (signals, manager,
    resources.py), ``class_prepared`` receivers included.

    MRO: RebacModelBase → RebacObjectMeta → ModelBase → type.
    ``super().__new__()`` in ``RebacObjectMeta`` chains through
    ``ModelBase.__new__()`` correctly.
    """

    def __new__(
        mcs, name: str, bases: tuple[type, ...], attrs: dict[str, Any], **kwargs: Any
    ) -> type:
        meta = attrs.get("Meta")
        options: dict[str, Any] = {}
        if meta is None:
            # Django gives a class with no Meta the one its bases expose. The Meta built
            # below would hide it, so its Django options are carried over; rebac_* options
            # are per model and travel only through a Meta the class writes itself.
            inherited = next(
                (cast(Any, base).Meta for base in bases if hasattr(base, "Meta")), None
            )
            options = {
                option: getattr(inherited, option)
                for option in DEFAULT_NAMES
                if option != "abstract" and hasattr(inherited, option)
            }
        base_manager_name = _declared_base_manager(meta, bases)
        if base_manager_name is None:
            base_manager_name = INJECTED_BASE_MANAGER
            attrs[INJECTED_BASE_MANAGER] = TrackedManager()
        options["base_manager_name"] = base_manager_name
        options["default_manager_name"] = getattr(
            meta, "default_manager_name", options.get("default_manager_name", "objects")
        )
        attrs["Meta"] = type("Meta", (meta,) if meta is not None else (), options)
        new_cls = cast(type[models.Model], super().__new__(mcs, name, bases, attrs, **kwargs))
        if not new_cls._meta.abstract:
            queryset_class = getattr(new_cls._meta.base_manager, "_queryset_class", None)
            if not (
                isinstance(queryset_class, type) and issubclass(queryset_class, TrackedQuerySet)
            ):
                raise ImproperlyConfigured(
                    f"{new_cls._meta.label} declares the base manager {base_manager_name!r}, "
                    "whose queryset is not a rebac.TrackedQuerySet. Writes through a model's "
                    "base manager must be gated: build the manager with "
                    "models.Manager.from_queryset() over a TrackedQuerySet subclass that "
                    "applies no actor scope and filters no rows."
                )
            from .signals import connect_owned_model

            connect_owned_model(new_cls)
        return new_cls

    @staticmethod
    def _carry_rebac_meta(attrs: dict[str, Any], captured: dict[str, Any]) -> None:
        attrs["_rebac_options"] = _CapturedOptions(captured)

    @staticmethod
    def _store_rebac_meta(target_cls: type[models.Model], captured: dict[str, Any]) -> None:
        """Nothing to store: ``_CapturedOptions`` put them on ``_meta`` already."""


class _CapturedOptions:
    """Puts the captured ``rebac_*`` options on ``_meta`` as Django builds the class.

    Django adds an attribute that has ``contribute_to_class`` after ``_meta``
    exists and before it sends ``class_prepared``, so a receiver of that
    signal reads the options like any other caller.
    """

    def __init__(self, captured: dict[str, Any]) -> None:
        self.captured = captured

    def contribute_to_class(self, cls: type[models.Model], name: str) -> None:
        for key, value in self.captured.items():
            setattr(cls._meta, key, value)


@dataclass
class DeleteScope:
    origin: Any
    using: str
    actor: SubjectRef | None
    unscoped: bool
    identities: set[ObjectRef] = dataclass_field(default_factory=set)
    root_pks: frozenset[Any] = frozenset()


_delete_scopes: ContextVar[tuple[DeleteScope, ...]] = ContextVar("rebac_delete_scopes", default=())
_insert_scope: ContextVar[tuple[Any, SubjectRef | None, bool] | None] = ContextVar(
    "rebac_insert_scope", default=None
)


def delete_scope(origin: Any, using: str) -> DeleteScope | None:
    return next(
        (
            scope
            for scope in reversed(_delete_scopes.get())
            if scope.origin is origin and scope.using == using
        ),
        None,
    )


@contextmanager
def deletion_owner(
    origin: Any, using: str, actor: SubjectRef | None, unscoped: bool
) -> Iterator[None]:
    from .managers import RebacQuerySet
    from .signals import cleanup_identities

    scope = DeleteScope(origin, using, actor, unscoped)
    if isinstance(origin, models.QuerySet):
        roots = (
            origin._system_capture(reason="rebac.delete.roots")
            if isinstance(origin, RebacQuerySet)
            else origin
        )
        scope.root_pks = frozenset(roots.values_list("pk", flat=True))
    token = _delete_scopes.set((*_delete_scopes.get(), scope))
    try:
        yield
        cleanup_identities(scope.identities, using=using)
    finally:
        _delete_scopes.reset(token)


@contextmanager
def insertion_scope(instance: Any, actor: SubjectRef | None, unscoped: bool) -> Iterator[None]:
    token = _insert_scope.set((instance, actor, unscoped))
    try:
        yield
    finally:
        _insert_scope.reset(token)


class RebacTrackedMixin(models.Model, metaclass=RebacModelBase):
    """Unscoped write ownership for first-party non-resource backing models."""

    objects: ClassVar[models.Manager[Any]] = TrackedManager()

    class Meta:
        abstract = True

    def save_base(
        self,
        raw: bool = False,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        from .signals import audit_backed_denials
        from .watch import model_write

        alias = using or router.db_for_write(type(self), instance=self)
        if raw:
            return super().save_base(raw, force_insert, force_update, alias, update_fields)
        with (
            audit_backed_denials(),
            model_write(model=type(self), using=alias, names=update_fields),
        ):
            from .signals import _gate_backed_field_change

            _gate_backed_field_change(type(self), self, alias, update_fields)
            super().save_base(raw, force_insert, force_update, alias, update_fields)

    def delete(
        self, using: str | None = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        from .actors import current_actor, is_sudo
        from .conf import app_settings
        from .signals import audit_backed_denials
        from .watch import model_write

        alias = using or router.db_for_write(type(self), instance=self)
        if delete_scope(self, alias) is not None:
            return super().delete(using=alias, keep_parents=keep_parents)
        actor = current_actor()
        unscoped = is_sudo() or (actor is None and not app_settings.REBAC_STRICT_MODE)
        with audit_backed_denials(), model_write(model=type(self), using=alias):
            from .signals import _gate_backed_field_change

            _gate_backed_field_change(type(self), self, alias, None, deleting=True)
            with deletion_owner(self, alias, actor, unscoped):
                return super().delete(using=alias, keep_parents=keep_parents)


class RebacMixin(RebacTrackedMixin):
    """Mix into a model to gate every read / write / delete on REBAC.

    Required: declare `Meta.rebac_resource_type = "<app>/<resource>"`.

    What this installs:
      - `objects = RebacManager()` — replaces the default manager.
      - `_default_manager` points at it; `_base_manager` owns gated writes
        and remains unfiltered for Django's relationship infrastructure.
      - save_base/delete owners gate writes; explicit-sender signals cover cascades.
      - ``from_db()`` propagates the queryset's actor onto loaded instances
        and snapshots loaded field values into ``_rebac_loaded_values`` for
        later dirty-field computation (per-field ``write__<f>`` enforcement;
        see ``mixins.py``).

    Instance-level surface (Odoo PR #179148 triple, plus actor / sudo binding):

      - ``instance.with_actor(actor)`` — pin actor for subsequent
        ``check_access`` / ``has_access`` / ``save()`` / ``delete()`` calls.
        Mirrors the queryset verb; ``as_user`` / ``as_agent`` are sugar
        over it.
      - ``instance.sudo(reason="...")`` — bypass REBAC on this instance only.
        Per CLAUDE.md § 5a the flag is non-transitive: FK / reverse-FK / M2M
        accessors must not inherit it. (Today no accessor reads the flag, so
        the invariant holds vacuously; the v1.x FK-accessor scoping work
        will need to keep ignoring ``_rebac_sudo_reason`` on traversal.)
      - ``instance.unsudo()`` — clear only the instance sudo pin, without
        binding an actor.
      - ``instance.is_sudo()`` / ``instance.actor()`` — introspection.
      - ``instance.check_access(action)`` — three-state ``CheckResult``.
      - ``instance.has_access(action)`` — boolean shorthand.

    Pickle / cross-process posture:
      ``__getstate__`` strips ``_rebac_actor``, ``_rebac_sudo_reason``, and
      ``_rebac_loaded_values`` before pickling. Instances crossing
      process / wire boundaries (e.g. ``apply_async(args=[instance])``)
      lose their pinned actor and sudo binding; the receiving worker MUST
      re-attach an actor via middleware / Celery hook before saving. This
      is fail-closed by design — actor identity must be re-asserted at
      every trust boundary, never silently inherited from a serialised
      blob.
    """

    # Django's model metaclass replaces the abstract parent's manager; the
    # class-level slot is intentionally more specific on resource models.
    objects: ClassVar[RebacManager] = RebacManager()  # pyright: ignore[reportIncompatibleVariableOverride]

    # Carried through from_db (via the queryset's `_fetch_all`) so
    # `instance.save()` re-checks against the same actor. Both this and
    # `_rebac_sudo_reason` are stripped by `__getstate__` below — pickled
    # instances arrive at the receiving process with NO actor / NO sudo,
    # forcing the consumer to re-attach via middleware / Celery hook.
    _rebac_actor: SubjectRef | None = None
    _rebac_sudo_reason: str | None = None
    _rebac_sudo_audited: bool = False
    _rebac_field_deny: FieldDenyMode | None = None
    _rebac_resource_id: str | None = None

    if TYPE_CHECKING:
        # Set on the instance by ``from_db`` (never class-level — a
        # class default would survive pickling and defeat the
        # actor-stripping contract in ``__getstate__``). Snapshot of the
        # row's loaded field values, used for per-field dirty detection
        # in save_base.
        _rebac_loaded_values: dict[str, Any]

    class Meta(RebacTrackedMixin.Meta):
        abstract = True

    def proposed_relationships(
        self, *, using: str | None = None
    ) -> Mapping[str, Iterable[SubjectRef | models.Model]]:
        """Relations this row will carry once persisted that are not derivable
        from its fields, keyed by relation name; consulted by the create gate
        before insert. The complementary owner for field-backed relations is
        ``rebac.field_backing._proposed_forward_relationships``; field-backed
        and const-backed relations belong to the library and must not be
        returned here. Use the supplied write database alias ``using`` when
        resolving subjects. Unreferenced subject iterables are not evaluated.

        This hook is trusted self-assertion. Contributing a fact the row does
        not actually carry after the write can silently grant access; omitting
        a fact it does carry can deny access. The gate does not verify these
        promises after writing. Persist the promised tuples in the same
        transaction. ``bulk_create()`` never calls ``save()``, so bulk paths
        must write the promised tuples themselves.

        Unknown, field-backed, or const-backed relation names raise
        ``SchemaError``. Subject resolution can raise ``NoActorResolvedError``
        for an unsaved or unresolvable proposed model instance.
        """
        return {}

    @classmethod
    def from_db(cls, db: Any, field_names: Any, values: Any) -> RebacMixin:
        """Instantiate from a row + snapshot loaded field values.

        The snapshot lives on ``instance._rebac_loaded_values`` and powers
        per-field write gates: the save_base owner compares it against the
        current values to detect dirty fields, then re-checks each one
        against ``write__<field>`` if such a permission is declared.

        Deferred fields are skipped (they aren't in ``field_names``); a
        subsequent ``refresh_from_db`` does NOT update the snapshot — that
        would defeat the audit purpose of "what was on the row when the
        actor first loaded it". Pure in-memory; no extra queries.
        """
        from django.db.models import DEFERRED

        instance = super().from_db(db, field_names, values)
        snapshot: dict[str, Any] = {}
        for name, value in zip(field_names, values, strict=False):
            if value is DEFERRED:
                continue
            snapshot[name] = value
        instance._rebac_loaded_values = snapshot
        try:
            instance._rebac_resource_id = str(getattr(instance, resource_id_attr(cls)))
        except Exception:
            pass
        return instance

    def refresh_from_db(
        self,
        using: str | None = None,
        fields: Iterable[str] | None = None,
        from_queryset: models.QuerySet[Any] | None = None,
    ) -> None:
        redacted = frozenset(getattr(self, "_rebac_redacted_fields", frozenset()) or frozenset())
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        if redacted:
            from .field_visibility import mark_denied_fields

            mode: FieldDenyMode = (
                "omit" if getattr(self, "_rebac_omitted_fields", frozenset()) else "redact"
            )
            mark_denied_fields(self, redacted, mode=mode)

    def __getstate__(self) -> dict[str, Any]:
        """Strip per-instance REBAC binding before pickling.

        Removes ``_rebac_actor``, ``_rebac_sudo_reason``, and
        ``_rebac_loaded_values`` so a pickled instance cannot smuggle a
        trusted actor across a process boundary. The receiving end must
        re-attach an actor via middleware / Celery hook (see CLAUDE.md
        § 5 — actor lives on the queryset / instance, not in a ContextVar
        that survives pickling).
        """
        state = super().__getstate__()
        if isinstance(state, dict):
            state.pop("_rebac_actor", None)
            state.pop("_rebac_sudo_reason", None)
            state.pop("_rebac_sudo_audited", None)
            state.pop("_rebac_loaded_values", None)
            state.pop("_rebac_field_deny", None)
        return state

    # ----- Actor / sudo binding -----

    def with_actor(self, actor: Any) -> Self:
        """Pin a SubjectRef on this instance. Returns self for chaining.

        Mirrors ``RebacQuerySet.with_actor`` — accepts any ``ActorLike``.
        Useful for hand-built instances (``Note(...).with_actor(u).save()``)
        that never flowed through a scoped queryset.

        Binding an actor clears any prior ``instance.sudo()`` on this
        instance — sudo and a pinned actor are mutually exclusive intents,
        same contract as the queryset.
        """
        from .actors import to_subject_ref

        self._rebac_actor = actor if isinstance(actor, SubjectRef) else to_subject_ref(actor)
        self._rebac_sudo_reason = None
        self._rebac_sudo_audited = False
        return self

    def as_user(self, user: Any) -> Self:
        """Typed shorthand: pin a Django ``User`` as the actor.

        Sugar for ``with_actor(to_subject_ref(user))`` — exactly one code
        path lives in ``with_actor``.
        """
        from .actors import to_subject_ref

        return self.with_actor(to_subject_ref(user))

    def as_agent(self, agent: Any, *, on_behalf_of: Any | None = None) -> Self:
        """Typed shorthand: pin an agent acting via a Grant.

        Sugar for ``with_actor(grant_subject_ref(agent, on_behalf_of))``.
        """
        from .actors import grant_subject_ref

        return self.with_actor(grant_subject_ref(agent, on_behalf_of))

    def sudo(self, *, reason: str) -> Self:
        """Bypass REBAC on this instance. Mandatory ``reason``.

        Scope is **this instance only** (CLAUDE.md § 5a). The bypass
        applies to the next ``save()`` / ``delete()`` / ``check_access``
        on this instance. FK / reverse-FK / M2M accessors do not — and
        must not — inherit the flag.
        """
        from .conf import app_settings
        from .errors import SudoNotAllowedError, SudoReasonRequiredError

        if not app_settings.REBAC_ALLOW_SUDO:
            raise SudoNotAllowedError("sudo() denied: REBAC_ALLOW_SUDO is False")
        if app_settings.REBAC_REQUIRE_SUDO_REASON and not reason:
            raise SudoReasonRequiredError(
                "sudo() requires reason= when REBAC_REQUIRE_SUDO_REASON=True"
            )
        self._rebac_sudo_reason = reason
        self._rebac_sudo_audited = False
        return self

    def unsudo(self) -> Self:
        """Clear this instance's sudo pin without binding an actor.

        Use ``with_actor(actor)`` when the intent is to bind a concrete actor
        and clear sudo in one step; ``unsudo()`` is only the inverse of
        ``sudo(reason=...)`` for actorless paths.
        """
        self._rebac_sudo_reason = None
        self._rebac_sudo_audited = False
        return self

    def _audit_bypass_once(self) -> None:
        if self._rebac_sudo_reason is None or self._rebac_sudo_audited:
            return
        from .actors import current_actor
        from .audit import emit
        from .models import PermissionAuditEvent

        actor = current_actor()
        emit(
            PermissionAuditEvent.KIND_SUDO_BYPASS,
            actor=actor,
            origin=actor,
            reason=self._rebac_sudo_reason,
            defer_to_commit=False,
        )
        self._rebac_sudo_audited = True

    def is_sudo(self) -> bool:
        return self._rebac_sudo_reason is not None

    def actor(self) -> SubjectRef | None:
        """Return the pinned actor on this instance, or ``None``."""
        return self._rebac_actor

    def effective_actor(self, *, strict: bool = False) -> tuple[SubjectRef | None, bool]:
        """Return ``(actor_ref, is_unscoped)`` for this instance.

        Explicit instance scope wins over ambient scope: per-instance sudo
        bypasses, a pinned actor scopes, ambient sudo only bypasses when no
        actor is pinned, and ambient ``current_actor()`` is the final scoped
        fallback. In observer mode (``strict=False``), a strict-mode no-actor
        state returns ``(None, False)`` instead of raising.
        """
        from .actors import current_actor as _current_actor
        from .actors import is_sudo as _is_sudo_ambient
        from .conf import app_settings
        from .errors import MissingActorError

        insert = _insert_scope.get()
        if insert is not None and insert[0] is self:
            return insert[1], insert[2]
        if self._rebac_sudo_reason is not None:
            if strict:
                self._audit_bypass_once()
            return (None, True)
        if self._rebac_actor is not None:
            return (self._rebac_actor, False)
        if _is_sudo_ambient():
            return (None, True)
        ambient = _current_actor()
        if ambient is not None:
            return (ambient, False)
        if app_settings.REBAC_STRICT_MODE:
            if strict:
                raise MissingActorError(
                    f"{type(self).__name__}.check_access() called with no actor. "
                    f"Use instance.with_actor(actor) or wrap in `with sudo(reason='...'):`."
                )
            return (None, False)
        return (None, True)

    # ----- Check API (Odoo PR #179148 triple) -----

    def check_access(
        self,
        action: str,
        *,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
    ) -> CheckResult:
        """Three-state check for ``action`` on this instance.

        Resolution order:
          1. Model not wired (no ``rebac_resource_type``) → ``HAS_PERMISSION``
             (mirrors the queryset's no-op behaviour).
          2. Resolve via ``effective_actor(strict=True)``:
             per-instance sudo → pinned actor → ambient sudo → ambient actor
             → strict-mode fallback.
          3. Unscoped/bypass resolution → ``HAS_PERMISSION``.
          5. Otherwise dispatch to the backend.
        """
        from .backends import backend

        rebac_type = model_resource_type(self)
        if not rebac_type:
            # Model isn't wired into REBAC — answer permissively to mirror
            # the manager's no-op behaviour.
            return CheckResult.has(reason="no resource type")

        actor, unscoped = self.effective_actor(strict=True)
        if unscoped:
            return CheckResult.has(reason="unscoped")
        assert actor is not None

        # Empty resource_id on adding is the backend's model-level "any row?"
        # sentinel. Persistence itself uses check_new's candidate overlay.
        if self._state.adding:
            resource_id = ""
        else:
            resource_id = self._rebac_resource_id_for_checks()
        return backend().check_access(
            subject=actor,
            action=action,
            resource=ObjectRef(rebac_type, resource_id),
            context=context,
            consistency=consistency,
        )

    def has_access(
        self,
        action: str,
        *,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
    ) -> bool:
        """Boolean shorthand. ``CONDITIONAL_PERMISSION`` collapses to ``False``."""
        return self.check_access(action, context=context, consistency=consistency).allowed

    # ----- Field read gates -----

    def with_field_deny(self, mode: FieldDenyMode) -> Self:
        """Override ``REBAC_FIELD_READ_MODE`` for explicit instance redaction."""
        from .field_visibility import validate_field_deny_mode, warn_raise_mode_degrades

        if mode == "raise":
            warn_raise_mode_degrades(stacklevel=2)
        self._rebac_field_deny = validate_field_deny_mode(mode)
        return self

    def denied_read_fields(
        self,
        *,
        context: dict[str, Any] | None = None,
    ) -> frozenset[str]:
        """Return gated field names the current actor may not read on this row."""
        from .conf import app_settings
        from .field_visibility import gated_read_fields
        from .types import PermissionResult

        denied: set[str] = set()
        for field_name in gated_read_fields(type(self)):
            result = self.check_access(f"read__{field_name}", context=context)
            if result.allowed:
                continue
            if (
                result.result is PermissionResult.CONDITIONAL_PERMISSION
                and not app_settings.REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL
            ):
                continue
            denied.add(field_name)
        return frozenset(denied)

    def redacted(
        self,
        *,
        mode: FieldDenyMode | None = None,
        context: dict[str, Any] | None = None,
    ) -> Self:
        """Apply the configured field deny mode to this instance in place."""
        from .field_visibility import (
            effective_field_deny_mode,
            mark_denied_fields,
            runtime_field_deny_mode,
            warn_raise_mode_degrades,
        )

        selected = effective_field_deny_mode(mode or self._rebac_field_deny)
        if mode == "raise":
            warn_raise_mode_degrades(stacklevel=2)
        runtime_mode = runtime_field_deny_mode(selected)
        if runtime_mode == "allow":
            return self
        mark_denied_fields(self, self.denied_read_fields(context=context), mode=runtime_mode)
        return self

    def save(self, *args: Any, **kwargs: Any) -> None:
        self._rebac_save(*args, **kwargs)

    def save_base(
        self,
        raw: bool = False,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        from .errors import PermissionDenied
        from .watch import model_write

        alias = using or router.db_for_write(type(self), instance=self)
        if raw:
            return super().save_base(raw, force_insert, force_update, alias, update_fields)
        try:
            with model_write(model=type(self), using=alias, names=update_fields):
                _gate_save(type(self), self, using=alias, update_fields=update_fields)
                super().save_base(raw, force_insert, force_update, alias, update_fields)
        except PermissionDenied as exc:
            # The gate's audit write was inside the rolled-back owner block
            # (model_write always opens one). Re-emit it after the rollback,
            # preserving any caller transaction and the exact field action.
            self._audit_denial_after_rollback(exc, default_action="write")
            raise

    def delete(
        self, using: str | None = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        from .errors import PermissionDenied
        from .watch import model_write

        alias = using or router.db_for_write(type(self), instance=self)
        scope = self.effective_actor(strict=bool(model_resource_type(self)))
        try:
            with model_write(model=type(self), using=alias):
                _gate_delete(type(self), self, scope=scope)
                with deletion_owner(self, alias, *scope):
                    # The tracked base reuses this scope, preserving consumer MRO.
                    return super().delete(using=alias, keep_parents=keep_parents)
        except PermissionDenied as exc:
            self._audit_denial_after_rollback(exc, default_action="delete")
            raise

    def _audit_denial_after_rollback(self, exc: Exception, *, default_action: str) -> None:
        from .conf import app_settings
        from .signals import BackedEdgeDenied, audit_edge_denial

        if isinstance(exc, BackedEdgeDenied):
            audit_edge_denial(exc)
            return

        if not app_settings.REBAC_AUDIT_DENIALS or " cannot " not in str(exc):
            return
        resource_type = model_resource_type(type(self))
        if resource_type is None:
            return
        actor, unscoped = self.effective_actor(strict=False)
        if unscoped:
            return
        action = str(exc).split(" cannot ", 1)[1].split(" ", 1)[0] or default_action
        resource_id = "" if self._state.adding else str(getattr(self, resource_id_attr(type(self))))
        _maybe_audit_denial(
            actor=actor,
            action=action,
            resource=ObjectRef(resource_type, resource_id),
        )

    def _rebac_save(self, *args: Any, **kwargs: Any) -> None:
        """Keep new resource instances insert-only and exclude redacted update fields.

        Explicit ``save(update_fields=[...])`` remains visible to the save_base
        owner, which fails closed if a redacted field is named.
        """
        if self._state.adding and model_resource_type(self):
            if kwargs.get("force_update") or kwargs.get("update_fields") is not None:
                raise ValueError(
                    "A new REBAC model instance must be inserted; "
                    "force_update and update_fields are not allowed."
                )
            _actor, unscoped = self.effective_actor(strict=True)
            if not unscoped:
                # Django otherwise attempts UPDATE first for populated primary
                # keys, including parent-table updates during multi-table
                # inheritance saves. An actor's child-create grant cannot
                # authorize those parent writes, so name every concrete table
                # explicitly. Django normalizes force_insert=True to the leaf
                # model only; _save_parents() forces a parent only when that
                # parent is present in this tuple.
                concrete_model = cast(type[models.Model], self._meta.concrete_model)
                kwargs["force_insert"] = (
                    *concrete_model._meta.all_parents,
                    concrete_model,
                )
        if not args and not self._state.adding and kwargs.get("update_fields") is None:
            redacted = frozenset(
                getattr(self, "_rebac_redacted_fields", frozenset()) or frozenset()
            )
            if redacted:
                kwargs["update_fields"] = self._rebac_update_fields_excluding(redacted)
        super().save(*args, **kwargs)

    def _rebac_update_fields_excluding(self, excluded: frozenset[str]) -> list[str]:
        names: list[str] = []
        loaded: dict[str, Any] | None = getattr(self, "_rebac_loaded_values", None)
        for field in type(self)._meta.concrete_fields:
            if field.primary_key:
                continue
            if field.name in excluded or field.attname in excluded:
                continue
            if loaded is not None:
                if field.attname not in loaded:
                    continue
                current = getattr(self, field.attname, None)
                if loaded[field.attname] == current:
                    continue
            names.append(field.name)
        return names

    def _rebac_resource_id_for_checks(self) -> str:
        attr = resource_id_attr(self)
        redacted = frozenset(getattr(self, "_rebac_redacted_fields", frozenset()) or frozenset())
        field_names = {attr}
        try:
            field = type(self)._meta.get_field(attr)
        except Exception:
            field = None
        if field is not None:
            field_names.add(field.name)
            field_names.add(getattr(field, "attname", field.name))
        if redacted & field_names:
            stored = getattr(self, "_rebac_resource_id", None)
            if stored is not None:
                return str(stored)
        return str(getattr(self, attr))


def _maybe_audit_denial(*, actor: SubjectRef | None, action: str, resource: ObjectRef) -> None:
    """Emit a denial audit row when REBAC_AUDIT_DENIALS is enabled.

    Owners call this after their failed atomic block unwinds. The row follows
    any still-open caller transaction and is durable when that transaction commits.

    Audit kind reuses the relevant grant / revoke kind (a denied write is a
    grant that didn't happen; a denied delete is a revoke that didn't
    happen). The reason text carries the ``denied:`` prefix so consumers
    can distinguish denial from successful writes when querying the trail.
    """
    if not app_settings.REBAC_AUDIT_DENIALS:
        return
    from .audit import emit as emit_audit
    from .models import PermissionAuditEvent

    if action == "delete":
        kind = PermissionAuditEvent.KIND_RELATIONSHIP_REVOKE
    else:
        kind = PermissionAuditEvent.KIND_RELATIONSHIP_GRANT
    emit_audit(
        kind,
        actor=actor,
        origin=actor,
        target_repr=f"{resource}#{action}",
        reason=f"denied: {actor} cannot {action} {resource}",
        defer_to_commit=False,
    )


def _gate_save(
    sender: type[models.Model],
    instance: Any,
    raw: bool = False,
    using: Any = None,
    update_fields: Iterable[str] | None = None,
    **_: Any,
) -> None:
    if raw:
        return
    if not isinstance(instance, RebacMixin):
        return
    rebac_type = model_resource_type(sender)
    if not rebac_type:
        return
    # Share the observer/check API's precedence: a pinned actor outranks
    # ambient sudo, while an explicit instance bypass still wins locally.
    actor, unscoped = instance.effective_actor(strict=True)
    if unscoped:
        return
    assert actor is not None

    is_create = instance._state.adding
    action = "create" if is_create else "write"

    from .backends import backend

    if is_create:
        active_backend = backend()
        result = _check_new_model(
            instance,
            subject=actor,
            using=using,
            backend=active_backend,
        )
        resource = ObjectRef(rebac_type, "")
    else:
        resource_id = _resource_id_for_existing_instance(sender=sender, instance=instance)
        resource = ObjectRef(rebac_type, resource_id)
        result = backend().check_access(subject=actor, action=action, resource=resource)
    if not result.allowed:
        _maybe_audit_denial(actor=actor, action=action, resource=resource)
        raise PermissionDenied(f"Denied: {actor} cannot {action} {resource}")

    # Per-field ``write__<f>`` enforcement — only on UPDATE. On INSERT the
    # row didn't exist, so "loaded values" is empty and every field is
    # trivially "dirty"; gating create on per-field permissions makes no
    # sense (use ``permission create = ...`` for that).
    if not is_create:
        _refuse_moving_edge(sender, instance, using=using or router.db_for_write(sender))
        _enforce_expression_reads(
            sender=sender,
            instance=instance,
            actor=actor,
            resource=resource,
            update_fields=update_fields,
        )
        _enforce_redacted_field_writes(
            sender=sender,
            instance=instance,
            resource=resource,
            update_fields=update_fields,
        )
        _enforce_per_field_writes(
            sender=sender,
            instance=instance,
            actor=actor,
            resource=resource,
            update_fields=update_fields,
        )


def _refuse_moving_edge(sender: type[models.Model], instance: Any, *, using: str) -> None:
    """Refuse a save that gives a polymorphic edge another target.

    The save writes the row its primary key names, so the target columns are
    compared with that row as stored (and locked), not with what was loaded.
    """
    from django.db.models.expressions import BaseExpression, Combinable

    from .signals import _stored
    from .watch import edge_columns, gate_policy, raise_moving_edge

    names = edge_columns(gate_policy(using), sender)
    if not names or instance.pk is None:
        return
    fields = {
        field for name in names if isinstance(field := sender._meta.get_field(name), models.Field)
    }
    attnames = sorted({field.attname for field in fields})
    stored = _stored(sender, using).filter(pk=instance.pk).values(*attnames).first()
    if stored is None:
        return
    changed = []
    for field in sorted(fields, key=lambda field: field.attname):
        value = getattr(instance, field.attname)
        if (
            isinstance(value, (BaseExpression, Combinable))
            or field.to_python(value) != stored[field.attname]
        ):
            changed.append(field.attname)
    if changed:
        raise_moving_edge(sender, changed)


def _gate_delete(
    sender: type[models.Model],
    instance: Any,
    scope: tuple[SubjectRef | None, bool] | None = None,
) -> None:
    if not isinstance(instance, RebacMixin):
        return
    rebac_type = model_resource_type(sender)
    if not rebac_type:
        return
    actor, unscoped = scope if scope is not None else instance.effective_actor(strict=True)
    if unscoped:
        return
    if actor is None:
        from .errors import MissingActorError

        raise MissingActorError("Cascaded delete has no actor.")

    from .backends import backend

    resource_id = str(getattr(instance, resource_id_attr(sender)))
    resource = ObjectRef(rebac_type, resource_id)
    result = backend().check_access(subject=actor, action="delete", resource=resource)
    if not result.allowed:
        _maybe_audit_denial(actor=actor, action="delete", resource=resource)
        raise PermissionDenied(f"Denied: {actor} cannot delete {resource}")


# ---------- Per-field write helpers ----------


def _enforce_expression_reads(
    *,
    sender: type[models.Model],
    instance: Any,
    actor: SubjectRef,
    resource: ObjectRef,
    update_fields: Iterable[str] | None,
) -> None:
    from django.db.models.sql import Query

    from .backends import backend
    from .field_visibility import (
        check_field_access,
        effective_field_deny_mode,
        gated_read_fields,
        runtime_field_deny_mode,
    )
    from .managers import (
        _column_on_model_lineage,
        _expression_columns,
        _has_gated_subquery,
        _has_opaque_write_expression,
    )

    gated = gated_read_fields(sender)
    selected = (
        set(_normalise_update_field_names(sender=sender, update_fields=update_fields))
        if update_fields is not None
        else {field.name for field in sender._meta.concrete_fields if not field.primary_key}
    )
    query = Query(sender)
    required: set[str] = set()
    for field in sender._meta.concrete_fields:
        if field.name not in selected:
            continue
        value = getattr(instance, field.attname)
        resolver = getattr(value, "resolve_expression", None)
        if not callable(resolver):
            continue
        if gated and _has_opaque_write_expression(value):
            raise PermissionDenied(
                "Write expression has opaque SQL over a model with gated fields."
            )
        if (
            runtime_field_deny_mode(effective_field_deny_mode(instance._rebac_field_deny))
            == "allow"
        ):
            continue
        resolved = resolver(query)
        if _has_gated_subquery(resolved):
            raise PermissionDenied("Write expression reads a subquery over gated fields.")
        required.update(
            column.target.name
            for column in _expression_columns(resolved)
            if _column_on_model_lineage(column, sender) and column.target.name in gated
        )
    for field_name in sorted(required):
        if not check_field_access(
            backend(),
            subject=actor,
            action=f"read__{field_name}",
            resource=resource,
        ).allowed:
            raise PermissionDenied(f"Write expression reads denied field read__{field_name}.")


def _enforce_redacted_field_writes(
    *,
    sender: type[models.Model],
    instance: Any,
    resource: ObjectRef,
    update_fields: Iterable[str] | None,
) -> None:
    redacted = frozenset(getattr(instance, "_rebac_redacted_fields", frozenset()) or frozenset())
    if not redacted or update_fields is None:
        return
    requested = set(_normalise_update_field_names(sender=sender, update_fields=update_fields))
    bad = redacted & requested
    if bad:
        names = ", ".join(sorted(bad))
        raise PermissionDenied(
            f"Cannot write redacted field(s) {names} on {resource}: "
            "read__<field> denied on the loaded instance."
        )


def _resource_id_for_existing_instance(*, sender: type[models.Model], instance: Any) -> str:
    attr = resource_id_attr(sender)
    redacted = frozenset(getattr(instance, "_rebac_redacted_fields", frozenset()) or frozenset())
    field_names = {attr}
    try:
        field = sender._meta.get_field(attr)
    except Exception:
        field = None
    if field is not None:
        field_names.add(field.name)
        field_names.add(getattr(field, "attname", field.name))
    if redacted & field_names:
        stored = getattr(instance, "_rebac_resource_id", None)
        if stored is not None:
            return str(stored)
    return str(getattr(instance, attr))


def _enforce_per_field_writes(
    *,
    sender: type[models.Model],
    instance: Any,
    actor: SubjectRef,
    resource: ObjectRef,
    update_fields: Iterable[str] | None,
) -> None:
    """Re-run ``check_access`` for any dirty field that has a ``write__<f>``
    permission declared on its resource type.

    Called after the resource-level ``write`` check has already passed.
    Honours ``save(update_fields=...)`` when supplied (the caller knows
    what's actually dirty); otherwise falls back to comparing current
    values against the snapshot ``from_db`` stashed on the instance. If
    no snapshot is present (instance hand-built and re-saved as an
    UPDATE — unusual), every non-pk concrete field is treated as dirty
    (conservative; fail-closed).

    Schema lookup goes via ``backend().schema()`` when the backend
    exposes one (LocalBackend always does; SpiceDBBackend will route
    through its own server-side schema once 0.5 lands). Backends without
    an in-process schema accessor skip per-field enforcement — the
    resource-level ``write`` check already gated the operation.

    Pure in-memory comparison; never queries the DB to refresh state.
    """
    schema = backend_schema()
    if schema is None:
        return
    definition = schema.get_definition(resource.resource_type)
    if definition is None:
        return
    declared = field_gated_actions(definition, "write")
    if not declared:
        return  # No per-field gates declared — common case, cheap exit.

    dirty = _dirty_field_names(sender=sender, instance=instance, update_fields=update_fields)
    if not dirty:
        return

    from .backends import backend

    for field_name in dirty:
        action = f"write__{field_name}"
        if action not in declared:
            continue  # Field inherits the resource-level write (already passed).
        result = backend().check_access(subject=actor, action=action, resource=resource)
        if not result.allowed:
            _maybe_audit_denial(actor=actor, action=action, resource=resource)
            raise PermissionDenied(
                f"Denied: {actor} cannot {action} {resource} "
                f"(field {field_name!r} requires {action})"
            )


def _dirty_field_names(
    *,
    sender: type[models.Model],
    instance: Any,
    update_fields: Iterable[str] | None,
) -> list[str]:
    """Return the list of (field.name) values that have changed.

    Trust order:

    1. If the caller passed ``save(update_fields=[...])``, trust it
       (Django itself only writes those columns). Normalise tokens to
       ``field.name`` so that both ``"folder"`` and ``"folder_id"`` look
       up ``write__folder`` correctly.
    2. Otherwise compare ``_rebac_loaded_values`` (snapshotted in
       ``RebacMixin.from_db``) against the current attribute values for
       every non-pk concrete field. Fields that were deferred at load
       time (absent from the snapshot) are treated as dirty.
    3. If no snapshot exists at all (e.g. hand-built instance being
       re-saved as an UPDATE — rare), conservatively treat every non-pk
       concrete field as dirty.
    """
    meta = sender._meta
    if update_fields is not None:
        return _normalise_update_field_names(sender=sender, update_fields=update_fields)

    concrete = [f for f in meta.concrete_fields if not f.primary_key]
    loaded: dict[str, Any] | None = getattr(instance, "_rebac_loaded_values", None)
    if loaded is None:
        return [f.name for f in concrete]

    dirty: list[str] = []
    for field in concrete:
        attname = field.attname
        current = getattr(instance, attname, None)
        if attname not in loaded:
            # Was deferred at load time — can't compare cheaply.
            dirty.append(field.name)
        elif loaded[attname] != current:
            dirty.append(field.name)
    return dirty


def _normalise_update_field_names(
    *,
    sender: type[models.Model],
    update_fields: Iterable[str],
) -> list[str]:
    meta = sender._meta
    names: list[str] = []
    for tok in update_fields:
        try:
            field = meta.get_field(tok)
        except Exception:
            # Unknown field tag — leave it; Django will reject the save.
            names.append(tok)
            continue
        names.append(field.name)
    return names
