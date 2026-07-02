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

from typing import TYPE_CHECKING, Any, Self

from django.db import models
from django.db.models.base import ModelBase

from ._id import resource_id_attr
from .managers import RebacManager
from .resources import model_resource_type
from .types import CheckResult, Consistency, FieldDenyMode, ObjectRef, SubjectRef

_RECOGNISED_META = (
    "rebac_resource_type",
    "rebac_default_action",
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
    for key in _RECOGNISED_META:
        if key in own:
            captured[key] = own[key]
            delattr(meta, key)
        elif hasattr(meta, key):
            captured[key] = getattr(meta, key)
    return captured


class RebacObjectMeta(type):
    """Registration metaclass for non-model REBAC resources (views, menus, etc.).

    Captures the same ``_RECOGNISED_META`` keys as ``RebacModelBase`` but
    stores them directly on the class as ``_rebac_<key>`` attributes rather
    than on ``._meta`` (which only exists on Django models).

    Usage::

        class FileListView(ListView):
            class Meta:
                rebac_resource_type = "angee/view"
                rebac_id_attr = "_angee_view_meta.source.operation"

        # After class creation:
        # FileListView._rebac_resource_type == "angee/view"
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

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        attrs: dict[str, Any],
        **kwargs: Any,
    ) -> type:
        captured = _capture_rebac_meta(attrs)
        new_cls = super().__new__(mcs, name, bases, attrs, **kwargs)
        mcs._store_rebac_meta(new_cls, captured)
        return new_cls

    @staticmethod
    def _store_rebac_meta(cls: type, captured: dict[str, Any]) -> None:
        # Keys are already named "rebac_*", so prefix with "_" only:
        # "rebac_resource_type" → "_rebac_resource_type"
        for key, value in captured.items():
            setattr(cls, f"_{key}", value)


class RebacModelBase(RebacObjectMeta, ModelBase):
    """Custom metaclass that strips ZED-specific Meta attrs before Django sees them.

    Inherits ``RebacObjectMeta`` for the capture logic and overrides
    ``_store_rebac_meta`` to stash values onto ``._meta`` so callers can still
    read them as ``<Model>._meta.rebac_resource_type`` (signals, manager,
    resources.py).

    MRO: RebacModelBase → RebacObjectMeta → ModelBase → type.
    ``super().__new__()`` in ``RebacObjectMeta`` chains through
    ``ModelBase.__new__()`` correctly.
    """

    @staticmethod
    def _store_rebac_meta(cls: type[models.Model], captured: dict[str, Any]) -> None:
        for key, value in captured.items():
            setattr(cls._meta, key, value)


class RebacMixin(models.Model, metaclass=RebacModelBase):
    """Mix into a model to gate every read / write / delete on REBAC.

    Required: declare `Meta.rebac_resource_type = "<app>/<resource>"`.

    What this installs:
      - `objects = RebacManager()` — replaces the default manager.
      - `_default_manager` points at it; `_base_manager` left unfiltered (Django
        uses base manager for FK reverse caching / M2M intermediates).
      - Pre-save / pre-delete signal handlers gate writes (wired in `signals.py`).
      - ``from_db()`` propagates the queryset's actor onto loaded instances
        and snapshots loaded field values into ``_rebac_loaded_values`` for
        later dirty-field computation (per-field ``write__<f>`` enforcement;
        see ``signals.py``).

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

    objects = RebacManager()

    # Carried through from_db (via the queryset's `_fetch_all`) so
    # `instance.save()` re-checks against the same actor. Both this and
    # `_rebac_sudo_reason` are stripped by `__getstate__` below — pickled
    # instances arrive at the receiving process with NO actor / NO sudo,
    # forcing the consumer to re-attach via middleware / Celery hook.
    _rebac_actor: SubjectRef | None = None
    _rebac_sudo_reason: str | None = None
    _rebac_field_deny: FieldDenyMode | None = None
    _rebac_resource_id: str | None = None

    if TYPE_CHECKING:
        # Set on the instance by ``from_db`` (never class-level — a
        # class default would survive pickling and defeat the
        # actor-stripping contract in ``__getstate__``). Snapshot of the
        # row's loaded field values, used for per-field dirty detection
        # in pre_save.
        _rebac_loaded_values: dict[str, Any]

    class Meta:
        abstract = True

    @classmethod
    def from_db(cls, db: Any, field_names: Any, values: Any) -> RebacMixin:
        """Instantiate from a row + snapshot loaded field values.

        The snapshot lives on ``instance._rebac_loaded_values`` and powers
        per-field write gates: ``signals.py`` compares it against the
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
        return self

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

        rebac_type = model_resource_type(type(self))
        if not rebac_type:
            # Model isn't wired into REBAC — answer permissively to mirror
            # the manager's no-op behaviour.
            return CheckResult.has(reason="no resource type")

        actor, unscoped = self.effective_actor(strict=True)
        if unscoped:
            return CheckResult.has(reason="unscoped")
        assert actor is not None

        # Empty resource_id on adding — same sentinel the pre-save signal
        # uses so the backend treats it as a model-level "any row?" check.
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
        """Exclude redacted fields from full-row updates.

        Explicit ``save(update_fields=[...])`` remains visible to the signal
        layer, which fails closed if a redacted field is named.
        """
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
        attr = resource_id_attr(type(self))
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
