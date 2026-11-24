"""Settings access. All values prefixed `REBAC_`. No nested dicts.

Read via the public `app_settings` proxy — never call `getattr(settings, ...)`
directly inside the package; use `app_settings.<KEY>`.

Every setting declares the values it accepts (``_TYPES``), and the proxy is the
one place that reads them. A value a setting does not accept never widens what
the library allows: a boolean reads its closed reading (a protection on, a
loosening or a mechanism that skips or shortens a permission statement off); a
number, a name, a path, the storage layout and the tracked models raise
``ImproperlyConfigured`` when read; the backend, the field read mode and the
Zookie transport pass to readers that refuse any value they do not know; and the
reserved settings the library does not read have no effect. A string is a
string: ``"False"`` is not ``False``. System checks name each such setting
(``rebac.E028``, ``W015``, or the setting's own check), and ``AppConfig.ready()``
logs each once, for a process that runs no checks.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed

_DEFAULTS: dict[str, Any] = {
    "REBAC_BACKEND": "local",
    "REBAC_TRACKED_MODELS": [],
    "REBAC_RELATIONSHIP_MODEL": "rebac.Relationship",
    "REBAC_SCHEMA_DIR": None,  # resolves to <cwd>/rebac at use site
    "REBAC_DEPTH_LIMIT": 8,
    # Compile an evaluator scope's statements for the stored grants its actor can hold (proposal 0019).
    "REBAC_ACTOR_PROFILES": True,
    # Read a prefetch's children without their permission statement where the policy proves
    # that the parents just admitted imply them (proposal 0020). Off: every prefetch is scoped.
    "REBAC_STRUCTURAL_PREFETCH": False,
    # Answer a bounded question (held, the select_related guard) by small lookups on the rows it
    # names, the compiled statement only for what they leave (proposal 0020). "prove" (or True)
    # settles the ids a grant on the row, a column or a parent shows held; "decide" reads the
    # whole permission and answers no as well.
    "REBAC_BOUNDED_WALK": False,
    # Decide a list read whose conditions name only keys the reader holds and select at most 256
    # rows by those rows: their keys are read without the permission, the walk decides them, and
    # the rows it admits are read by key (proposal 0025). Experimental. True: only where the walk
    # reads nothing of those rows but their keys. "identifiers": also where it reads the foreign
    # keys it follows and the subjects of their grants. Off: every list read carries its
    # compiled scope.
    "REBAC_BOUNDED_READ": False,
    # Write a queryset's scope as conditions on each of its rows instead of membership in the
    # ids the actor holds (proposal 0020). "named": only where the queryset's own conditions bind
    # its primary key or a foreign key to literal values. True: for every read the policy allows.
    "REBAC_ROW_SCOPE": False,
    # Deliver a related row that rebac_select_related loaded with its declared display fields
    # (Meta.rebac_display_fields) where the actor may not read the row (proposal 0020).
    # "fallback": instead of raising. True: always and without the check, for a model that
    # declares display fields.
    "REBAC_DISPLAY_FIELDS": False,
    # Convert a stored id of an integer identity to its key by the cast alone, without the
    # guard that passes over an id that is not the key's canonical text (proposal 0020). For
    # an application whose relationship rows are all written with canonical ids. A row that
    # is not then makes PostgreSQL raise on the statement, and SQLite read it by its numeric
    # value ("01" as 1).
    "REBAC_TRUSTED_IDS": False,
    "REBAC_DEFAULT_CONSISTENCY": "minimize_latency",
    "REBAC_CACHE_ALIAS": "default",
    "REBAC_LOOKUP_CACHE_TTL": 60,
    "REBAC_STRICT_MODE": True,
    # False: no permission is checked. Every caller, with an actor or without, reads and writes
    # every row; no scope, guard or field gate is applied and no permission statement is issued.
    # For a deployment with one trusted user, and for measuring an application's queries without
    # the library's. Relationships and the policy are still stored. System check rebac.W011.
    "REBAC_ENABLED": True,
    "REBAC_REQUIRE_SUDO_REASON": True,
    "REBAC_ALLOW_SUDO": True,
    "REBAC_GC_INTERVAL_SECONDS": 300,
    "REBAC_AUTHENTICATION_MIDDLEWARE": "django.contrib.auth.middleware.AuthenticationMiddleware",
    "REBAC_ACTOR_RESOLVER": "rebac.actors.default_resolver",
    # Dotted-path callable resolving an MCP request Context → SubjectRef, used
    # by the ``rebac.mcp.rebac_mcp_tool`` decorator. Its result is the per-call
    # actor and takes priority over the ambient ``current_actor()`` (explicit
    # per-request identity wins; ambient is the fallback). The default reads the
    # canonical SubjectRef string at ``ctx.request_context.meta["actor_subject"]``.
    # Override for transports that carry the actor elsewhere (a signed header, a
    # session).
    "REBAC_MCP_ACTOR_RESOLVER": "rebac.mcp.default_actor_resolver",
    "REBAC_TYPE_PREFIX": "",
    "REBAC_SUPERUSER_BYPASS": True,
    "REBAC_USER_TYPE": "auth/user",
    "REBAC_GROUP_TYPE": "auth/group",
    # Subject type representing an unauthenticated request. The default
    # resolver returns ``SubjectRef.of(REBAC_ANONYMOUS_TYPE, "*")`` when
    # ``request.user.is_authenticated`` is False, and ``to_subject_ref``
    # returns the same when handed Django's ``AnonymousUser``. Schemas
    # reference it as the type wildcard (``auth/anonymous:*``) on a
    # relation, or via the bare ``anonymous`` schema keyword in a
    # permission expression. Both forms match the same subject.
    "REBAC_ANONYMOUS_TYPE": "auth/anonymous",
    # Universal-admin role checked by the ``rebac.W004`` system check.
    # Every ``<namespace>/role`` definition is expected to include this
    # role's ``#member`` subject-set in its ``member`` relation's type
    # union so the role acts as an "all roles" override.
    #
    # Opt in with an application-owned role reference; the standalone engine
    # must not assume a consumer's namespace or admin policy.
    "REBAC_UNIVERSAL_ADMIN_ROLE": None,
    # Where the engine sources resource ids when a model doesn't set
    # ``Meta.rebac_id_attr``. ``"pk"`` is the historical default;
    # consumers shipping public-id fields (sqid, public_id, slug) flip
    # this globally without touching every model.
    "REBAC_RESOURCE_ID_ATTR": "pk",
    # Same idea for the actor side of ``to_subject_ref`` when the
    # actor is a Django ``User`` / ``Group`` instance. Per-model
    # ``Meta.rebac_id_attr`` still wins when set.
    "REBAC_USER_ID_ATTR": "pk",
    # When True, the save/delete gates also write a
    # PermissionAuditEvent row before raising PermissionDenied. Defaults to
    # False because every denied write doubles as a failed-attempt log row,
    # which can dominate the audit table on heavy denial traffic (e.g. an
    # attacker fuzzing IDs). Switch on for high-stakes deployments where
    # forensic trails of attempted writes outweigh the volume cost.
    "REBAC_AUDIT_DENIALS": False,
    # Structural lint that walks every RBAC-bound model and emits a
    # ``rebac.W003`` ``checks.Warning`` for every FK / O2O / M2M
    # whose target is also RBAC-bound. The check fires for the
    # *existence* of the relation, not for any actual bare-string
    # ``select_related`` / ``prefetch_related`` usage — so it surfaces the JOIN-leak
    # surface but cannot tell whether callers are already wrapping
    # those joins in ``rebac_select_related`` / ``rebac_prefetch_related``.
    # Enabled by default because the unsafe shape should be visible unless
    # a project has audited and accepted every occurrence.
    "REBAC_LINT_BARE_PREFETCH": True,
    # LocalBackend storage shape. ``"denormalized"`` (the current and default
    # 0.4 shape) stores ``resource_type / resource_id / subject_type /
    # subject_id`` as wide ``CharField`` columns on every ``Relationship``
    # row — the wire shape that mirrors ``authzed.api.v1.Relationship``.
    # ``"registry"`` stores
    # those four columns as integer FKs into a shared ``RebacResource``
    # table, yielding a 5-10x index-density gain on the hot path and
    # FK-CASCADE cleanup when the underlying Django row is deleted.
    #
    # Migration between the two is one-shot via
    # ``python manage.py rebac migrate-storage --to registry``. The wire
    # shape (``RelationshipTuple`` + string kwargs to the public manager)
    # is unchanged in either mode.
    "REBAC_LOCAL_BACKEND_STORAGE": "denormalized",
    # Batch size for ``rebac migrate-storage`` when streaming rows between
    # the two shapes. Lower this on tight-memory hosts; raise it on big
    # transactional DBs where round-trip cost dominates per-row work.
    "REBAC_LOCAL_BACKEND_REGISTRY_BATCH_SIZE": 5000,
    # Total entries in the ``check`` cache
    # the per-request :class:`rebac.evaluator.PermissionEvaluator` holds
    # before evicting LRU. 10_000 covers the largest realistic GraphQL
    # query fan-out without risk of OOM under adversarial input. Raise
    # for workloads that genuinely benefit (rare); lower on
    # tight-memory hosts.
    "REBAC_EVALUATOR_CACHE_SIZE": 10_000,
    # Field-level read gate behavior for schema permissions named
    # ``read__<field>``. ``"allow"`` is the additive/default-off posture;
    # ``"raise"`` is accepted for forwards compatibility and degrades to
    # ``"redact"`` until the descriptor-based protected-fields tier lands.
    "REBAC_FIELD_READ_MODE": "allow",
    "REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL": True,
    # Cross-request transport for the post-write ``Zookie`` so
    # subsequent reads upgrade to ``at_least_as_fresh``:
    #   - ``"none"``    — single-request scope only (default).
    #   - ``"header"``  — request/response header
    #     (``REBAC_ZOOKIE_HEADER_NAME``). Natural for SPAs / JWT.
    #   - ``"session"`` — ``request.session`` key
    #     (``REBAC_ZOOKIE_SESSION_KEY``). Requires
    #     ``django.contrib.sessions`` (system check ``rebac.W006``).
    "REBAC_ZOOKIE_TRANSPORT": "none",
    "REBAC_ZOOKIE_HEADER_NAME": "X-Rebac-Zookie",
    "REBAC_ZOOKIE_SESSION_KEY": "_rebac_zookie",
}


_RAISE: Any = object()


@dataclass(frozen=True, slots=True)
class _Type:
    """The values a setting accepts and how a value it does not accept is read.

    ``expected`` says what to write. ``closed`` is the reading of a value it does
    not accept, or ``_RAISE`` where no reading is safe. ``report`` is the system
    check that names such a value. ``passes`` hands the value to its readers as
    written, with ``reads_as`` saying what they do with it: a setting whose
    readers refuse or ignore any value they do not know, and one the library does
    not read at all (``inert``).
    """

    expected: str
    accepts: Callable[[Any], bool]
    closed: Any = _RAISE
    report: str = "rebac.E028"
    passes: bool = False
    reads_as: str = ""
    inert: bool = False


def _is_bool(value: Any) -> bool:
    return value is True or value is False


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _bool(closed: bool) -> _Type:
    return _Type("True or False", _is_bool, closed)


def _switch(*names: str, also: tuple[str, ...] = ()) -> _Type:
    """True, False or one of ``names``; ``also`` are spellings read but not documented."""
    spelled = " or ".join(f'"{name}"' for name in names)
    known = (*names, *also)
    return _Type(
        f"True, False or {spelled}",
        lambda value: _is_bool(value) or (isinstance(value, str) and value in known),
        False,
    )


def _one_of(*names: str, report: str, **kwargs: Any) -> _Type:
    return _Type(
        " or ".join(f'"{name}"' for name in names),
        lambda value: isinstance(value, str) and value in names,
        report=report,
        **kwargs,
    )


def _labels(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(label, str) for label in value)


def _inert(kind: _Type) -> _Type:
    return _Type(
        kind.expected,
        kind.accepts,
        report="rebac.W015",
        passes=True,
        reads_as="the library does not read it, so it has no effect",
        inert=True,
    )


_INT = _Type("an int", _is_int)
_STR = _Type("a str", _is_str)
_OPTIONAL_STR = _Type("a str or None", lambda value: value is None or isinstance(value, str))
_PATH = _Type(
    "a str, a pathlib.Path or None",
    lambda value: value is None or isinstance(value, (str, os.PathLike)),
)


# One entry per setting in ``_DEFAULTS``. A bool or switch that loosens protection, or lets the
# library skip or shorten a permission statement, reads a value it does not accept as off; one
# that protects, audits or lints reads it as on. Names, numbers, dotted paths, types and the
# storage layout have no safe reading: guessing one could name other subjects, resolve actors
# another way or read another table.
_TYPES: dict[str, _Type] = {
    # backend() raises ValueError for any other value.
    "REBAC_BACKEND": _one_of(
        "local", report="rebac.E001", passes=True, reads_as="backend() raises ValueError"
    ),
    "REBAC_TRACKED_MODELS": _Type("a list or tuple of str", _labels, report="rebac.E018"),
    "REBAC_RELATIONSHIP_MODEL": _inert(_STR),
    "REBAC_SCHEMA_DIR": _PATH,
    "REBAC_DEPTH_LIMIT": _INT,
    "REBAC_ACTOR_PROFILES": _bool(closed=False),
    "REBAC_STRUCTURAL_PREFETCH": _bool(closed=False),
    "REBAC_BOUNDED_WALK": _switch("prove", "decide"),
    "REBAC_BOUNDED_READ": _switch("identifiers"),
    "REBAC_ROW_SCOPE": _switch("named", also=("always",)),
    "REBAC_DISPLAY_FIELDS": _switch("fallback", also=("always",)),
    "REBAC_TRUSTED_IDS": _bool(closed=False),
    "REBAC_DEFAULT_CONSISTENCY": _inert(_STR),
    "REBAC_CACHE_ALIAS": _inert(_STR),
    "REBAC_LOOKUP_CACHE_TTL": _inert(_INT),
    "REBAC_STRICT_MODE": _bool(closed=True),
    "REBAC_ENABLED": _bool(closed=True),
    "REBAC_REQUIRE_SUDO_REASON": _bool(closed=True),
    "REBAC_ALLOW_SUDO": _bool(closed=False),
    "REBAC_GC_INTERVAL_SECONDS": _inert(_INT),
    "REBAC_AUTHENTICATION_MIDDLEWARE": _STR,
    "REBAC_ACTOR_RESOLVER": _STR,
    "REBAC_MCP_ACTOR_RESOLVER": _STR,
    "REBAC_TYPE_PREFIX": _OPTIONAL_STR,
    "REBAC_SUPERUSER_BYPASS": _bool(closed=False),
    "REBAC_USER_TYPE": _STR,
    "REBAC_GROUP_TYPE": _STR,
    "REBAC_ANONYMOUS_TYPE": _STR,
    "REBAC_UNIVERSAL_ADMIN_ROLE": _OPTIONAL_STR,
    "REBAC_RESOURCE_ID_ATTR": _STR,
    "REBAC_USER_ID_ATTR": _STR,
    "REBAC_AUDIT_DENIALS": _bool(closed=True),
    "REBAC_LINT_BARE_PREFETCH": _bool(closed=True),
    # Read per query: a value read as the other layout would read and write a stale table.
    "REBAC_LOCAL_BACKEND_STORAGE": _one_of("denormalized", "registry", report="rebac.E006"),
    "REBAC_LOCAL_BACKEND_REGISTRY_BATCH_SIZE": _INT,
    "REBAC_EVALUATOR_CACHE_SIZE": _INT,
    # Every field gate validates the mode and raises ValueError for any other value.
    "REBAC_FIELD_READ_MODE": _one_of(
        "allow",
        "redact",
        "omit",
        "raise",
        report="rebac.E008",
        passes=True,
        reads_as="every field read gate raises ValueError",
    ),
    "REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL": _bool(closed=True),
    # Any other value transports no Zookie: freshness across requests only, no permission.
    "REBAC_ZOOKIE_TRANSPORT": _one_of(
        "none",
        "header",
        "session",
        report="rebac.E007",
        passes=True,
        reads_as='it is read as "none"',
    ),
    "REBAC_ZOOKIE_HEADER_NAME": _STR,
    "REBAC_ZOOKIE_SESSION_KEY": _STR,
}


@dataclass(frozen=True, slots=True)
class _WronglyTyped:
    """A setting whose configured value is not one it accepts."""

    name: str
    value: Any
    kind: _Type
    default: Any

    @property
    def expected(self) -> str:
        return self.kind.expected

    @property
    def closed(self) -> Any:
        return self.kind.closed

    @property
    def report(self) -> str:
        return self.kind.report

    @property
    def raises(self) -> bool:
        return not self.kind.passes and self.kind.closed is _RAISE

    def problem(self) -> str:
        """What is wrong: the setting, the value's type and the value, and what it accepts."""
        shown = repr(self.value)
        if len(shown) > 60:
            shown = shown[:57] + "..."
        detail = ""
        if self.kind.accepts is _labels and isinstance(self.value, (list, tuple)):
            # A lazy translation string prints as the str it stands for: name the item's type.
            index, item = next(
                (index, item) for index, item in enumerate(self.value) if not isinstance(item, str)
            )
            detail = f" (item {index} is {type(item).__name__} {item!r})"
        return f"{self.name} is {type(self.value).__name__} {shown}{detail}, not {self.expected}"

    def reading(self) -> str:
        """What the library does with the value."""
        if self.kind.passes:
            return self.kind.reads_as
        if self.raises:
            return "every read of it raises ImproperlyConfigured"
        return f"it is read as {self.closed!r}"


class _WrongSettingType(ImproperlyConfigured):
    """A setting whose value is not one it accepts, and has no safe reading."""

    def __init__(self, entry: _WronglyTyped) -> None:
        hint = ""
        if entry.name == "REBAC_TRACKED_MODELS" and isinstance(entry.value, str):
            hint = ' A one-label tuple needs its comma: ("app_label.Model",).'
        super().__init__(f"{entry.problem()} ({entry.report}).{hint}")
        self.entry = entry


def _configured(name: str) -> Any:
    return getattr(settings, name, _DEFAULTS[name])


def _wrongly_typed(name: str, value: Any) -> _WronglyTyped | None:
    kind = _TYPES[name]
    if kind.accepts(value):
        return None
    return _WronglyTyped(name, value, kind, _DEFAULTS[name])


def _wrongly_typed_settings() -> list[_WronglyTyped]:
    """Every setting whose configured value is not one it accepts, by name."""
    found = (_wrongly_typed(name, _configured(name)) for name in sorted(_TYPES))
    return [entry for entry in found if entry is not None]


class _AppSettings:
    """Lazy proxy. Reads from `django.conf.settings`, falls back to defaults.

    A value the setting does not accept reads as the setting's closed reading,
    raises ``_WrongSettingType``, or passes to readers that refuse it (see the
    module docstring). Cached values invalidate on `setting_changed` signal
    (test ergonomics).
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        setting_changed.connect(self._on_changed)

    def _on_changed(self, sender: Any, setting: str, value: Any, **kwargs: Any) -> None:
        if setting in _DEFAULTS:
            self._cache.pop(setting, None)
        if setting in {"REBAC_TRACKED_MODELS", "AUTH_USER_MODEL"}:
            from django.apps import apps

            if apps.ready:
                from .signals import connect_tracked_signals

                try:
                    connect_tracked_signals()
                except _WrongSettingType:
                    # Only an override changes a setting in a running process; the receivers
                    # stay as they were. A process that starts with the value stops in setup.
                    pass

    def problem(self, name: str) -> _WronglyTyped | None:
        """Whether ``name``'s configured value is one it accepts, without reading it."""
        if name not in _DEFAULTS:
            raise AttributeError(f"Unknown REBAC setting: {name!r}")
        return _wrongly_typed(name, _configured(name))

    def __getattr__(self, name: str) -> Any:
        if name not in _DEFAULTS:
            raise AttributeError(f"Unknown REBAC setting: {name!r}")
        if name in self._cache:
            return self._cache[name]
        value = _configured(name)
        wrong = _wrongly_typed(name, value)
        if wrong is not None and not wrong.kind.passes:
            if wrong.raises:
                raise _WrongSettingType(wrong)
            value = wrong.closed
        self._cache[name] = value
        return value

    def reset(self) -> None:
        self._cache.clear()


app_settings = _AppSettings()
