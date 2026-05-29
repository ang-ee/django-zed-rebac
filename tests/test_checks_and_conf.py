"""Tests for system checks and settings cache behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from django.core import checks
from django.test import override_settings

from rebac.checks import (
    check_cross_rbac_relations,
    check_field_read_mode_setting,
    check_universal_admin_in_roles,
)
from rebac.conf import app_settings


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
    # Hint references the explicit Prefetch form.
    for issue in w003:
        assert issue.hint is not None
        assert "Prefetch(" in issue.hint
        assert ".with_actor(actor)" in issue.hint


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


def test_w004_warns_when_role_definition_missing_universal_admin():
    _set_schema_via_localbackend(
        """
        definition auth/user {}
        definition angee/role {
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


def test_w004_silent_when_universal_admin_present():
    _set_schema_via_localbackend(
        """
        definition auth/user {}
        definition angee/role {
            relation member: auth/user
        }
        definition storage/role {
            relation member: auth/user | auth/group#member | angee/role:admin#member
        }
        """
    )
    issues = check_universal_admin_in_roles()
    w004 = [i for i in issues if i.id == "rebac.W004"]
    assert w004 == []


def test_w004_skips_the_universal_admin_role_itself():
    # The universal-admin role doesn't reference itself; no self-loop
    # warning.
    _set_schema_via_localbackend(
        """
        definition auth/user {}
        definition angee/role {
            relation member: auth/user
        }
        """
    )
    issues = check_universal_admin_in_roles()
    w004 = [i for i in issues if i.id == "rebac.W004"]
    assert not any("angee/role" in i.msg for i in w004), [i.msg for i in w004]


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


def test_w004_skips_non_role_definitions():
    # storage/file isn't a role — should be ignored by the check.
    _set_schema_via_localbackend(
        """
        definition auth/user {}
        definition angee/role {
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
