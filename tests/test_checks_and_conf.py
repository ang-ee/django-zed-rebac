"""Tests for system checks and settings cache behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from django.core import checks
from django.test import override_settings

from rebac.checks import (
    check_auth_backend_installed,
    check_backend_setting,
    check_cross_rbac_relations,
    check_field_read_mode_setting,
    check_universal_admin_in_roles,
)
from rebac.conf import app_settings


def test_write_alias_check_accepts_read_replica_but_rejects_split_writes(monkeypatch):
    from django.apps import apps
    from django.db import router

    from rebac import checks as rebac_checks
    from rebac.schema.ast import Schema
    from tests.testapp.models import Post

    monkeypatch.setattr(rebac_checks, "_schema_on_alias", lambda using: Schema())
    monkeypatch.setattr(apps, "get_models", lambda **kwargs: [Post])
    monkeypatch.setattr(router, "db_for_read", lambda model, **kwargs: "replica")
    monkeypatch.setattr(router, "db_for_write", lambda model, **kwargs: "default")
    assert not any(
        issue.id == "rebac.E015"
        for issue in rebac_checks.check_policy_models(databases=["default"])
    )
    monkeypatch.setattr(
        router, "db_for_write", lambda model, **kwargs: "other" if model is Post else "default"
    )
    assert any(
        issue.id == "rebac.E015"
        for issue in rebac_checks.check_policy_models(databases=["default"])
    )


@pytest.mark.parametrize("kind", ["BooleanField", "DateField", "DecimalField"])
def test_dynamic_container_unsupported_scalar_codecs_are_explicit(kind):
    from django.db import models
    from django.test.utils import isolate_apps

    from rebac.codec import identity_codec
    from rebac.errors import SchemaError

    with isolate_apps("tests.testapp"):
        field = getattr(models, kind)(
            **({"max_digits": 10, "decimal_places": 2} if kind == "DecimalField" else {})
        )

        class ContainerSubject(models.Model):
            container = field

            class Meta:
                app_label = "testapp"

        with pytest.raises(SchemaError, match=rf"rebac.E014.*container.*{kind}"):
            identity_codec(ContainerSubject, "container")


def test_app_settings_cache_invalidation_on_override_settings():
    assert app_settings.REBAC_BACKEND == "local"
    with override_settings(REBAC_BACKEND="spicedb"):
        assert app_settings.REBAC_BACKEND == "spicedb"
    assert app_settings.REBAC_BACKEND == "local"


def test_actor_middleware_must_be_after_authentication_middleware():
    with override_settings(
        MIDDLEWARE=[
            "rebac.middleware.ActorMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ]
    ):
        errors = checks.run_checks(tags=["rebac"])
    ids = {issue.id for issue in errors}
    assert "rebac.E004" in ids


def test_actor_middleware_requires_authentication_middleware_present():
    with override_settings(MIDDLEWARE=["rebac.middleware.ActorMiddleware"]):
        errors = checks.run_checks(tags=["rebac"])
    ids = {issue.id for issue in errors}
    assert "rebac.E003" in ids


def test_actor_middleware_order_uses_configured_authentication_middleware():
    auth_path = "example.auth.AuthenticationMiddleware"
    with override_settings(
        REBAC_AUTHENTICATION_MIDDLEWARE=auth_path,
        MIDDLEWARE=[
            auth_path,
            "rebac.middleware.ActorMiddleware",
        ],
    ):
        errors = checks.run_checks(tags=["rebac"])
    ids = {issue.id for issue in errors}
    assert "rebac.E003" not in ids
    assert "rebac.E004" not in ids


def test_actor_middleware_order_errors_against_configured_authentication_middleware():
    auth_path = "example.auth.AuthenticationMiddleware"
    with override_settings(
        REBAC_AUTHENTICATION_MIDDLEWARE=auth_path,
        MIDDLEWARE=[
            "rebac.middleware.ActorMiddleware",
            auth_path,
        ],
    ):
        errors = checks.run_checks(tags=["rebac"])
    ids = {issue.id for issue in errors}
    assert "rebac.E004" in ids


def test_e008_rejects_invalid_field_read_mode():
    with override_settings(REBAC_FIELD_READ_MODE="explode"):
        issues = check_field_read_mode_setting()
    ids = {issue.id for issue in issues}
    assert "rebac.E008" in ids


def test_w008_warns_that_raise_field_read_mode_is_reserved():
    with override_settings(REBAC_FIELD_READ_MODE="raise"):
        issues = check_field_read_mode_setting()
    ids = {issue.id for issue in issues}
    assert "rebac.W008" in ids


# ---------------------------------------------------------------------------
# rebac.W003 — cross-RBAC FK/M2M warning
# ---------------------------------------------------------------------------


@override_settings(REBAC_LINT_BARE_PREFETCH=True)
def test_w003_fires_for_rbac_to_rbac_fk_in_testapp():
    """``Post.folder`` (RBAC) → ``Folder`` (RBAC) and ``Folder.parent``
    (self-FK) both fire W003 against the real testapp models."""
    issues = check_cross_rbac_relations()
    w003 = [i for i in issues if i.id == "rebac.W003"]
    messages = [i.msg for i in w003]
    # Post.folder → Folder (both RBAC-bound).
    assert any("testapp.Post.folder" in m and "testapp.Folder" in m for m in messages), messages
    # Folder.parent → Folder (self-FK, both RBAC-bound).
    assert any("testapp.Folder.parent" in m and "testapp.Folder" in m for m in messages), messages
    # Hint references the permission-aware relation-loading helpers.
    for issue in w003:
        assert issue.hint is not None
        assert "rebac_select_related" in issue.hint
        assert "rebac_prefetch_related" in issue.hint


def test_w003_on_by_default_for_rbac_related_fields():
    """The structural RBAC-to-RBAC relation warning is enabled by default."""
    issues = check_cross_rbac_relations()
    assert any(i.id == "rebac.W003" for i in issues)


@override_settings(REBAC_LINT_BARE_PREFETCH=False)
def test_w003_can_be_disabled_explicitly():
    assert check_cross_rbac_relations() == []


def _make_field(name, related_model, kind):
    """Build a duck-typed field instance whose ``isinstance`` check matches
    one of (FK, O2O, M2M) so the system check accepts it."""
    from django.db import models

    base: Any = {
        "fk": models.ForeignKey,
        "o2o": models.OneToOneField,
        "m2m": models.ManyToManyField,
    }[kind]
    # Build a minimal subclass that bypasses Field.__init__ — we only need
    # isinstance() and the .name / .related_model attrs.
    instance = base.__new__(base)
    instance.name = name
    instance.related_model = related_model
    return instance


def _make_model(app_label, name, *, rebac_type, fields):
    """Build a duck-typed model surrogate with the attributes the check reads."""
    meta = SimpleNamespace(
        app_label=app_label,
        rebac_resource_type=rebac_type,
        get_fields=lambda: fields,
    )
    cls = type(name, (), {"_meta": meta, "__name__": name})
    return cls


@override_settings(REBAC_LINT_BARE_PREFETCH=True)
def test_w003_does_not_fire_for_fk_to_non_rbac_model():
    """An RBAC-bound model with an FK to a non-RBAC target (e.g. ``auth.User``)
    must NOT trip W003."""
    non_rbac = _make_model("auth", "User", rebac_type=None, fields=[])
    fk_field = _make_field("author", non_rbac, "fk")
    rbac_model = _make_model("blog", "Article", rebac_type="blog/article", fields=[fk_field])

    with patch("django.apps.apps.get_models", return_value=[rbac_model, non_rbac]):
        issues = check_cross_rbac_relations()

    w003 = [i for i in issues if i.id == "rebac.W003"]
    # No W003 against blog.Article.author should appear.
    assert not any("blog.Article.author" in i.msg for i in w003), [i.msg for i in w003]


@override_settings(REBAC_LINT_BARE_PREFETCH=True)
def test_w003_does_not_fire_for_non_rbac_model_pointing_at_rbac():
    """The warning is about RBAC→RBAC traversal only. A non-RBAC source
    pointing at an RBAC target must NOT trip W003."""
    rbac_target = _make_model("blog", "Article", rebac_type="blog/article", fields=[])
    fk_field = _make_field("article", rbac_target, "fk")
    non_rbac_source = _make_model("stats", "PageView", rebac_type=None, fields=[fk_field])

    with patch("django.apps.apps.get_models", return_value=[rbac_target, non_rbac_source]):
        issues = check_cross_rbac_relations()

    w003 = [i for i in issues if i.id == "rebac.W003"]
    # Nothing should be emitted from the non-RBAC source.
    assert not any("stats.PageView" in i.msg for i in w003), [i.msg for i in w003]


# ---------------------------------------------------------------------------
# rebac.W004 — universal-admin entry in <namespace>/role definitions
# ---------------------------------------------------------------------------


def _set_schema_via_localbackend(schema_text):
    """Install a schema directly onto the singleton backend's in-memory cache.

    Bypasses DB sync — sufficient for testing the W004 walk over
    `backend().schema()`. The check itself is agnostic to where
    the schema came from.
    """
    from rebac.backends import LocalBackend, backend, reset_backend
    from rebac.schema import parse_zed

    reset_backend()
    active = backend()
    assert isinstance(active, LocalBackend)
    active.set_schema(parse_zed(schema_text))


@override_settings(REBAC_UNIVERSAL_ADMIN_ROLE="platform/role:admin")
def test_w004_warns_when_role_definition_missing_universal_admin():
    _set_schema_via_localbackend(
        """
        definition auth/user {}
        definition platform/role {
            relation member: auth/user
        }
        definition storage/role {
            relation member: auth/user | auth/group#member
        }
        """
    )
    issues = check_universal_admin_in_roles()
    ids = {i.id for i in issues}
    assert "rebac.W004" in ids
    w004 = [i for i in issues if i.id == "rebac.W004"]
    assert any("storage/role" in i.msg for i in w004)


@override_settings(REBAC_UNIVERSAL_ADMIN_ROLE="platform/role:admin")
def test_w004_silent_when_universal_admin_present():
    _set_schema_via_localbackend(
        """
        definition auth/user {}
        definition platform/role {
            relation member: auth/user
        }
        definition storage/role {
            relation member: auth/user | auth/group#member | platform/role:admin#member
        }
        """
    )
    issues = check_universal_admin_in_roles()
    w004 = [i for i in issues if i.id == "rebac.W004"]
    assert w004 == []


@override_settings(REBAC_UNIVERSAL_ADMIN_ROLE="platform/role:admin")
def test_w004_skips_the_universal_admin_role_itself():
    # The universal-admin role doesn't reference itself; no self-loop
    # warning.
    _set_schema_via_localbackend(
        """
        definition auth/user {}
        definition platform/role {
            relation member: auth/user
        }
        """
    )
    issues = check_universal_admin_in_roles()
    w004 = [i for i in issues if i.id == "rebac.W004"]
    assert not any("platform/role" in i.msg for i in w004), [i.msg for i in w004]


def test_w004_disabled_when_setting_is_none():
    _set_schema_via_localbackend(
        """
        definition auth/user {}
        definition storage/role {
            relation member: auth/user | auth/group#member
        }
        """
    )
    with override_settings(REBAC_UNIVERSAL_ADMIN_ROLE=None):
        issues = check_universal_admin_in_roles()
    w004 = [i for i in issues if i.id == "rebac.W004"]
    assert w004 == []


@override_settings(REBAC_UNIVERSAL_ADMIN_ROLE="platform/role:admin")
def test_w004_skips_non_role_definitions():
    # storage/file isn't a role — should be ignored by the check.
    _set_schema_via_localbackend(
        """
        definition auth/user {}
        definition platform/role {
            relation member: auth/user
        }
        definition storage/file {
            relation owner: auth/user
            permission read = owner
        }
        """
    )
    issues = check_universal_admin_in_roles()
    w004 = [i for i in issues if i.id == "rebac.W004"]
    assert not any("storage/file" in i.msg for i in w004)


def test_w004_errors_on_malformed_setting():
    _set_schema_via_localbackend("definition auth/user {}")
    with override_settings(REBAC_UNIVERSAL_ADMIN_ROLE="missing-colon"):
        issues = check_universal_admin_in_roles()
    e005 = [i for i in issues if i.id == "rebac.E005"]
    assert e005, [i.id for i in issues]


def test_universal_admin_is_opt_in():
    assert app_settings.REBAC_UNIVERSAL_ADMIN_ROLE is None
    assert check_universal_admin_in_roles() == []


# ---------------------------------------------------------------------------
# Settings checks — ARCHITECTURE.md § AppConfig and system checks
# ---------------------------------------------------------------------------


def _ids(issues):
    return [issue.id for issue in issues]


@pytest.mark.parametrize("value", ["bogus", "", "LOCAL"])
def test_e001_rejects_unknown_backend(value):
    with override_settings(REBAC_BACKEND=value):
        issues = check_backend_setting()
    assert _ids(issues).count("rebac.E001") == 1
    assert all(issue.level == checks.ERROR for issue in issues if issue.id == "rebac.E001")


@pytest.mark.parametrize("value", ["local", "spicedb"])
def test_e001_silent_for_supported_backends(value):
    with override_settings(
        REBAC_BACKEND=value, REBAC_SPICEDB_ENDPOINT="localhost:50051", REBAC_SPICEDB_TOKEN="t"
    ):
        assert "rebac.E001" not in _ids(check_backend_setting())


def test_e001_is_reported_by_the_check_framework():
    with override_settings(REBAC_BACKEND="bogus"):
        issues = checks.run_checks(tags=["rebac"])
    assert "rebac.E001" in _ids(issues)


def test_e002_requires_spicedb_endpoint():
    with override_settings(
        REBAC_BACKEND="spicedb", REBAC_SPICEDB_ENDPOINT=None, REBAC_SPICEDB_TOKEN="t"
    ):
        issues = [i for i in check_backend_setting() if i.id == "rebac.E002"]
    assert sum("REBAC_SPICEDB_ENDPOINT" in issue.msg for issue in issues) == 1


def test_e002_requires_spicedb_token():
    with override_settings(
        REBAC_BACKEND="spicedb", REBAC_SPICEDB_ENDPOINT="localhost:50051", REBAC_SPICEDB_TOKEN=""
    ):
        issues = [i for i in check_backend_setting() if i.id == "rebac.E002"]
    assert sum("REBAC_SPICEDB_TOKEN" in issue.msg for issue in issues) == 1


@pytest.mark.parametrize("installed", [False, True])
def test_e002_requires_client_even_when_spicedb_settings_are_configured(monkeypatch, installed):
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if installed else None,
    )
    with override_settings(
        REBAC_BACKEND="spicedb", REBAC_SPICEDB_ENDPOINT="localhost:50051", REBAC_SPICEDB_TOKEN="t"
    ):
        issues = [issue for issue in check_backend_setting() if issue.id == "rebac.E002"]
        assert bool(issues) is not installed
        assert all("authzed" in issue.msg for issue in issues)
    with override_settings(REBAC_BACKEND="local", REBAC_SPICEDB_ENDPOINT=None):
        assert "rebac.E002" not in _ids(check_backend_setting())


def test_e002_is_reported_by_the_check_framework():
    with override_settings(REBAC_BACKEND="spicedb", REBAC_SPICEDB_ENDPOINT=None):
        issues = checks.run_checks(tags=["rebac"])
    assert "rebac.E002" in _ids(issues)


def test_e006_rejects_unknown_storage():
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="columnar"):
        ids = _ids(check_backend_setting())
    assert ids.count("rebac.E006") == 1
    assert "rebac.W005" not in ids


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_e006_silent_for_supported_storage(storage):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        assert "rebac.E006" not in _ids(check_backend_setting())


def test_w005_recommends_registry_for_local_denormalized_storage():
    with override_settings(REBAC_BACKEND="local", REBAC_LOCAL_BACKEND_STORAGE="denormalized"):
        issues = [i for i in check_backend_setting() if i.id == "rebac.W005"]
    assert len(issues) == 1
    assert issues[0].level == checks.WARNING
    assert "migrate-storage" in (issues[0].hint or "")


def test_w005_silent_for_registry_storage_or_other_backend():
    with override_settings(REBAC_BACKEND="local", REBAC_LOCAL_BACKEND_STORAGE="registry"):
        assert "rebac.W005" not in _ids(check_backend_setting())
    with override_settings(
        REBAC_BACKEND="spicedb",
        REBAC_LOCAL_BACKEND_STORAGE="denormalized",
        REBAC_SPICEDB_ENDPOINT="localhost:50051",
        REBAC_SPICEDB_TOKEN="t",
    ):
        assert "rebac.W005" not in _ids(check_backend_setting())


def test_w001_warns_without_rebac_auth_backend():
    with override_settings(AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"]):
        issues = check_auth_backend_installed()
    assert _ids(issues) == ["rebac.W001"]
    assert issues[0].level == checks.WARNING


@pytest.mark.parametrize(
    "path", ["rebac.backends.auth.RebacBackend", "rebac.backends.RebacBackend"]
)
def test_w001_silent_with_rebac_auth_backend(path):
    with override_settings(
        AUTHENTICATION_BACKENDS=[path, "django.contrib.auth.backends.ModelBackend"]
    ):
        assert check_auth_backend_installed() == []


def test_w001_not_silenced_by_a_foreign_class_named_rebac_backend():
    with override_settings(AUTHENTICATION_BACKENDS=["example.auth.RebacBackend"]):
        assert _ids(check_auth_backend_installed()) == ["rebac.W001"]


def test_w101_is_a_deploy_only_check():
    from django.core.checks.registry import registry

    from rebac.checks import check_production_settings

    assert check_production_settings in registry.get_checks(include_deployment_checks=True)
    assert check_production_settings not in registry.get_checks(include_deployment_checks=False)


def test_w101_warns_when_spicedb_tls_is_disabled():
    from rebac.checks import check_production_settings

    with override_settings(REBAC_BACKEND="spicedb", REBAC_SPICEDB_TLS=False):
        issues = check_production_settings()
    assert _ids(issues) == ["rebac.W101"]
    assert issues[0].level == checks.WARNING


def test_w101_silent_with_tls_or_local_backend():
    from rebac.checks import check_production_settings

    with override_settings(REBAC_BACKEND="spicedb", REBAC_SPICEDB_TLS=True):
        assert check_production_settings() == []
    with override_settings(REBAC_BACKEND="local", REBAC_SPICEDB_TLS=False):
        assert check_production_settings() == []


@pytest.mark.parametrize("transport", ["none", "header"])
def test_e007_and_w006_silent_for_stateless_transports(transport):
    from rebac.checks import check_zookie_transport_setting

    with override_settings(REBAC_ZOOKIE_TRANSPORT=transport):
        assert check_zookie_transport_setting() == []


def test_w006_warns_for_session_transport_without_sessions_app():
    from django.apps import apps

    from rebac.checks import check_zookie_transport_setting

    assert not apps.is_installed("django.contrib.sessions")
    with override_settings(REBAC_ZOOKIE_TRANSPORT="session"):
        assert _ids(check_zookie_transport_setting()) == ["rebac.W006"]


def test_w006_silent_for_session_transport_with_sessions_app():
    from django.apps import apps
    from django.test import modify_settings

    from rebac.checks import check_zookie_transport_setting

    with (
        override_settings(REBAC_ZOOKIE_TRANSPORT="session"),
        modify_settings(INSTALLED_APPS={"append": "django.contrib.sessions"}),
    ):
        assert apps.is_installed("django.contrib.sessions")
        assert check_zookie_transport_setting() == []


@pytest.mark.parametrize("mode", ["allow", "redact", "omit", "raise"])
def test_e008_silent_for_supported_field_read_modes(mode):
    with override_settings(REBAC_FIELD_READ_MODE=mode):
        assert "rebac.E008" not in _ids(check_field_read_mode_setting())
