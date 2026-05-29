"""Settings access. All values prefixed `REBAC_`. No nested dicts.

Read via the public `app_settings` proxy — never call `getattr(settings, ...)`
directly inside the package; use `app_settings.<KEY>`.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.test.signals import setting_changed

_DEFAULTS: dict[str, Any] = {
    "REBAC_BACKEND": "local",
    "REBAC_RELATIONSHIP_MODEL": "rebac.Relationship",
    "REBAC_SPICEDB_ENDPOINT": None,
    "REBAC_SPICEDB_TOKEN": None,
    "REBAC_SPICEDB_TLS": True,
    "REBAC_SPICEDB_AUTO_WRITE_SCHEMA": True,
    "REBAC_SCHEMA_DIR": None,  # resolves to <cwd>/rebac at use site
    "REBAC_DEPTH_LIMIT": 8,
    "REBAC_DEFAULT_CONSISTENCY": "minimize_latency",
    "REBAC_CACHE_ALIAS": "default",
    "REBAC_LOOKUP_CACHE_TTL": 60,
    "REBAC_PK_IN_THRESHOLD": 10000,
    "REBAC_STRICT_MODE": True,
    "REBAC_REQUIRE_SUDO_REASON": True,
    "REBAC_ALLOW_SUDO": True,
    "REBAC_GC_INTERVAL_SECONDS": 300,
    "REBAC_AUTHENTICATION_MIDDLEWARE": "django.contrib.auth.middleware.AuthenticationMiddleware",
    "REBAC_ACTOR_RESOLVER": "rebac.actors.default_resolver",
    "REBAC_TYPE_PREFIX": "",
    "REBAC_SUPERUSER_BYPASS": True,
    "REBAC_SYNC_DJANGO_GROUPS": False,
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
    # Set to ``None`` to disable the W004 check entirely (security-locked
    # environments where the universal-admin tier is unacceptable).
    #
    # Default ``"angee/role:admin"`` matches the role shipped by
    # ``angee.auth`` in the angee-django framework.
    "REBAC_UNIVERSAL_ADMIN_ROLE": "angee/role:admin",
    # Where the engine sources resource ids when a model doesn't set
    # ``Meta.rebac_id_attr``. ``"pk"`` is the historical default;
    # consumers shipping public-id fields (sqid, public_id, slug) flip
    # this globally without touching every model.
    "REBAC_RESOURCE_ID_ATTR": "pk",
    # Same idea for the actor side of ``to_subject_ref`` when the
    # actor is a Django ``User`` / ``Group`` instance. Per-model
    # ``Meta.rebac_id_attr`` still wins when set.
    "REBAC_USER_ID_ATTR": "pk",
    # When True, the pre-save / pre-delete signal handlers also write a
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
    # ``prefetch_related`` usage — so it surfaces the JOIN-leak
    # surface but cannot tell whether callers are already wrapping
    # those JOINs in ``Prefetch(queryset=Related.objects.with_actor)``.
    # Enabled by default because the unsafe shape should be visible unless
    # a project has audited and accepted every occurrence.
    # When the engine grows true auto-scoping for bare-string
    # prefetch (planned), this check goes away entirely.
    "REBAC_LINT_BARE_PREFETCH": True,
    # LocalBackend storage shape. ``"denormalized"`` (the current and default
    # 0.4 shape) stores ``resource_type / resource_id / subject_type /
    # subject_id`` as wide ``CharField`` columns on every ``Relationship``
    # row — the wire shape that mirrors ``authzed.api.v1.Relationship``.
    # ``"registry"`` (proposal 0001, opt-in in 0.4, default in 0.5) stores
    # those four columns as integer FKs into a shared ``RebacResource``
    # table, yielding a 5-10x index-density gain on the hot path and
    # FK-CASCADE cleanup when the underlying Django row is deleted.
    #
    # Migration between the two is one-shot via
    # ``python manage.py rebac migrate-storage --to registry``. The wire
    # shape (``RelationshipTuple`` + string kwargs to the public manager)
    # is unchanged in either mode. Setting affects ``LocalBackend`` only;
    # ``SpiceDBBackend`` is unaffected.
    "REBAC_LOCAL_BACKEND_STORAGE": "denormalized",
    # Batch size for ``rebac migrate-storage`` when streaming rows between
    # the two shapes. Lower this on tight-memory hosts; raise it on big
    # transactional DBs where round-trip cost dominates per-row work.
    "REBAC_LOCAL_BACKEND_REGISTRY_BATCH_SIZE": 5000,
    # Total entries (across both ``check`` and ``accessible`` caches)
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


class _AppSettings:
    """Lazy proxy. Reads from `django.conf.settings`, falls back to defaults.

    Cached values invalidate on `setting_changed` signal (test ergonomics).
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        setting_changed.connect(self._on_changed)

    def _on_changed(self, sender: Any, setting: str, value: Any, **kwargs: Any) -> None:
        if setting in _DEFAULTS:
            self._cache.pop(setting, None)

    def __getattr__(self, name: str) -> Any:
        if name not in _DEFAULTS:
            raise AttributeError(f"Unknown REBAC setting: {name!r}")
        if name in self._cache:
            return self._cache[name]
        value = getattr(settings, name, _DEFAULTS[name])
        self._cache[name] = value
        return value

    def reset(self) -> None:
        self._cache.clear()


app_settings = _AppSettings()
