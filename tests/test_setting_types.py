"""Settings are read by their declared type: a value they do not accept never widens anything.

A deployment that passes settings through an environment variable gets strings, and
``"False"`` is truthy. Each setting either reads such a value as its closed reading (a
protection on, a loosening off, a mechanism that skips or shortens a permission
statement off), raises ``ImproperlyConfigured`` where no reading is safe, or hands it to
readers that refuse it; a check names it (ARCHITECTURE.md § Settings catalog).

The readings are written out below, not read from ``rebac.conf``: the table is the
specification the module is held to. The tests after the tables pin the readers.
"""

from __future__ import annotations

import ast
import io
import logging
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import RequestFactory, override_settings

import rebac
from rebac import sudo
from rebac.backends.auth import RebacBackend
from rebac.checks import (
    _CHECK_IDS,
    check_backend_setting,
    check_permissions_enforced,
    check_setting_types,
    check_tracked_models_setting,
    check_universal_admin_in_roles,
)
from rebac.conf import _DEFAULTS, _TYPES, _wrongly_typed_settings, app_settings
from rebac.errors import MissingActorError, SudoNotAllowedError, SudoReasonRequiredError
from tests.test_bounded_walk import local as local
from tests.test_structural_prefetch import rows as rows

# The reading of a wrongly typed value, for every setting that has one.
CLOSED = {
    # Protections stay on.
    "REBAC_ENABLED": True,
    "REBAC_STRICT_MODE": True,
    "REBAC_REQUIRE_SUDO_REASON": True,
    "REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL": True,
    # Loosenings stay off.
    "REBAC_ALLOW_SUDO": False,
    "REBAC_SUPERUSER_BYPASS": False,
    "REBAC_TRUSTED_IDS": False,
    "REBAC_DISPLAY_FIELDS": False,
    # Mechanisms that skip or shorten a permission statement stay off.
    "REBAC_STRUCTURAL_PREFETCH": False,
    "REBAC_ACTOR_PROFILES": False,
    "REBAC_ROW_SCOPE": False,
    "REBAC_BOUNDED_WALK": False,
    # Auditing and linting stay on.
    "REBAC_AUDIT_DENIALS": True,
    "REBAC_LINT_BARE_PREFETCH": True,
}
# No safe reading: a number, a name, a dotted path, a type. Reading it raises; E028 reports it.
RAISES = {
    "REBAC_DEPTH_LIMIT": "an int",
    "REBAC_EVALUATOR_CACHE_SIZE": "an int",
    "REBAC_LOCAL_BACKEND_REGISTRY_BATCH_SIZE": "an int",
    "REBAC_AUTHENTICATION_MIDDLEWARE": "a str",
    "REBAC_ACTOR_RESOLVER": "a str",
    "REBAC_MCP_ACTOR_RESOLVER": "a str",
    "REBAC_USER_TYPE": "a str",
    "REBAC_GROUP_TYPE": "a str",
    "REBAC_ANONYMOUS_TYPE": "a str",
    "REBAC_RESOURCE_ID_ATTR": "a str",
    "REBAC_USER_ID_ATTR": "a str",
    "REBAC_ZOOKIE_HEADER_NAME": "a str",
    "REBAC_ZOOKIE_SESSION_KEY": "a str",
    "REBAC_TYPE_PREFIX": "a str or None",
    "REBAC_UNIVERSAL_ADMIN_ROLE": "a str or None",
    "REBAC_SCHEMA_DIR": "a str, a pathlib.Path or None",
}
# No safe reading either, and reported by a check of their own.
RAISES_OWN = {
    "REBAC_LOCAL_BACKEND_STORAGE": "rebac.E006",
    "REBAC_TRACKED_MODELS": "rebac.E018",
}
# Passed to readers that refuse any value they do not know; reported by a check of their own.
REFUSED_BY_READERS = {
    "REBAC_BACKEND": "rebac.E001",
    "REBAC_FIELD_READ_MODE": "rebac.E008",
    "REBAC_ZOOKIE_TRANSPORT": "rebac.E007",
}
# Documented, never read: a wrong type is a warning (W015).
INERT = {
    "REBAC_RELATIONSHIP_MODEL": "a str",
    "REBAC_DEFAULT_CONSISTENCY": "a str",
    "REBAC_CACHE_ALIAS": "a str",
    "REBAC_LOOKUP_CACHE_TTL": "an int",
    "REBAC_GC_INTERVAL_SECONDS": "an int",
}

BOOL_WRONG = ["False", "True", "false", "true", "0", "1", "", 0, 1, None]
SWITCH_WRONG = ["False", "True", "unknown", 0, 1, None]
WRONG = {
    "an int": ["8", 8.0, True, None],
    "a str": [None, 5, b"text", ["x"]],
    "a str or None": [5, b"text", ["x"]],
    "a str, a pathlib.Path or None": [5, ["x"]],
}
SWITCHES = {"REBAC_DISPLAY_FIELDS", "REBAC_ROW_SCOPE", "REBAC_BOUNDED_WALK"}
STORAGE_WRONG = [None, b"registry", "Registry", ["registry"], "columnar"]
TRACKED_WRONG = [
    "testapp.BackingStage",  # ("testapp.BackingStage") without its comma
    {"testapp.BackingStage"},
    frozenset({"testapp.BackingStage"}),
    [b"testapp.BackingStage"],
    None,
]


def _closed_cases():
    for name, closed in sorted(CLOSED.items()):
        for value in SWITCH_WRONG if name in SWITCHES else BOOL_WRONG:
            yield pytest.param(name, value, closed, id=f"{name}={value!r}")


def _raising_cases():
    for name, expected in sorted(RAISES.items()):
        for value in WRONG[expected]:
            yield pytest.param(name, value, expected, id=f"{name}={value!r}")


def _inert_cases():
    for name, expected in sorted(INERT.items()):
        for value in WRONG[expected]:
            yield pytest.param(name, value, expected, id=f"{name}={value!r}")


def _e028(name):
    return [issue for issue in check_setting_types() if issue.msg.startswith(f"{name} is ")]


def test_every_setting_is_classified():
    groups = [CLOSED, RAISES, RAISES_OWN, REFUSED_BY_READERS, INERT]
    assert set().union(*groups) == set(_DEFAULTS) == set(_TYPES)
    assert sum(map(len, groups)) == len(_DEFAULTS)


def test_the_defaults_and_the_test_settings_are_well_typed():
    assert check_setting_types() == []
    assert _wrongly_typed_settings() == []
    for name, default in _DEFAULTS.items():
        assert _TYPES[name].accepts(default), name


@pytest.mark.parametrize(("name", "value", "closed"), _closed_cases())
def test_a_wrongly_typed_boolean_reads_closed_and_is_reported(name, value, closed):
    with override_settings(**{name: value}):
        assert getattr(app_settings, name) is closed
        [issue] = _e028(name)
    assert issue.id == "rebac.E028"
    assert issue.level == checks.ERROR
    assert issue.msg.startswith(f"{name} is {type(value).__name__} {value!r}, not ")
    assert issue.msg.endswith(f"it is read as {closed!r}.")
    assert f"Write {name} as " in issue.hint


@pytest.mark.parametrize(("name", "value", "expected"), _raising_cases())
def test_a_wrongly_typed_value_without_a_safe_reading_raises_and_is_reported(name, value, expected):
    with override_settings(**{name: value}):
        with pytest.raises(ImproperlyConfigured, match=f"{name} is {type(value).__name__} "):
            getattr(app_settings, name)
        [issue] = _e028(name)
    assert issue.id == "rebac.E028"
    assert issue.level == checks.ERROR
    assert issue.msg == (
        f"{name} is {type(value).__name__} {value!r}, not {expected}: "
        "every read of it raises ImproperlyConfigured."
    )


@pytest.mark.parametrize(("name", "value", "expected"), _inert_cases())
def test_a_wrongly_typed_unread_setting_is_a_warning_that_says_it_is_unread(name, value, expected):
    with override_settings(**{name: value}):
        assert getattr(app_settings, name) == value
        [issue] = [i for i in check_setting_types() if i.msg.startswith(f"{name} is ")]
    assert issue.id == "rebac.W015"
    assert issue.level == checks.WARNING
    assert issue.msg == (
        f"{name} is {type(value).__name__} {value!r}, not {expected}: "
        "the library does not read it, so it has no effect."
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REBAC_ROW_SCOPE", "named"),
        ("REBAC_DISPLAY_FIELDS", "fallback"),
        ("REBAC_BOUNDED_WALK", "prove"),
        ("REBAC_BOUNDED_WALK", "decide"),
        ("REBAC_TYPE_PREFIX", None),
        ("REBAC_UNIVERSAL_ADMIN_ROLE", "platform/role:admin"),
        ("REBAC_DEPTH_LIMIT", 12),
        ("REBAC_LOCAL_BACKEND_STORAGE", "registry"),
        ("REBAC_TRACKED_MODELS", ("testapp.BackingStage",)),
    ],
)
def test_a_value_of_the_declared_type_is_read_as_written(name, value):
    with override_settings(**{name: value}):
        assert getattr(app_settings, name) == value
        assert _wrongly_typed_settings() == []


# ---------- Storage and tracked models: no safe reading, their own checks ----------


@pytest.mark.django_db
@pytest.mark.parametrize("value", STORAGE_WRONG)
def test_a_storage_layout_it_does_not_know_raises_at_every_query(value):
    from rebac.models import active_relationship_model

    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=value):
        with pytest.raises(ImproperlyConfigured, match="REBAC_LOCAL_BACKEND_STORAGE"):
            active_relationship_model()
        with pytest.raises(ImproperlyConfigured, match="REBAC_LOCAL_BACKEND_STORAGE"):
            rebac.backend().check_access(
                subject=rebac.SubjectRef.of("auth/user", "a"),
                action="read",
                resource=rebac.ObjectRef("blog/post", "1"),
            )
        [issue] = [i for i in check_backend_setting() if i.id == "rebac.E006"]
        assert _e028("REBAC_LOCAL_BACKEND_STORAGE") == []
    assert issue.level == checks.ERROR
    assert f"REBAC_LOCAL_BACKEND_STORAGE={value!r}" in issue.msg


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("value", [None, "Registry"])
def test_a_mistyped_storage_after_a_cutover_reads_neither_table(settings, value):
    """The stale denormalized table keeps a grant the registry revoked; it is never read."""
    from rebac import ObjectRef, RelationshipTuple, SubjectRef
    from rebac.backends import reset_backend
    from rebac.relationships import delete_relationship
    from rebac.testing import install_schema

    schema = """
    definition auth/user {}
    definition test/doc {
        relation viewer: auth/user
        permission view = viewer
    }
    """
    alice, doc = SubjectRef.of("auth/user", "alice"), ObjectRef("test/doc", "d")
    grant = RelationshipTuple(doc, "viewer", alice)

    def view():
        return rebac.backend().check_access(subject=alice, action="view", resource=doc).allowed

    settings.REBAC_LOCAL_BACKEND_STORAGE = "denormalized"
    reset_backend()
    install_schema(schema).write_relationships([grant])
    call_command("rebac", "migrate-storage", "--to", "registry", stdout=io.StringIO())
    settings.REBAC_LOCAL_BACKEND_STORAGE = "registry"
    reset_backend()
    install_schema(schema)
    delete_relationship(grant)
    assert view() is False
    settings.REBAC_LOCAL_BACKEND_STORAGE = value
    with pytest.raises(ImproperlyConfigured, match="REBAC_LOCAL_BACKEND_STORAGE"):
        view()
    with pytest.raises(ImproperlyConfigured, match="REBAC_LOCAL_BACKEND_STORAGE"):
        install_schema(schema)
    settings.REBAC_LOCAL_BACKEND_STORAGE = "denormalized"
    reset_backend()


@pytest.mark.parametrize("value", TRACKED_WRONG, ids=repr)
def test_tracked_models_it_does_not_accept_raise_where_receivers_are_connected(value):
    from rebac import signals
    from tests.testapp.models import BackingStage

    with override_settings(REBAC_TRACKED_MODELS=value):
        with pytest.raises(ImproperlyConfigured, match="REBAC_TRACKED_MODELS"):
            signals.tracked_model(BackingStage)
        with pytest.raises(ImproperlyConfigured, match="REBAC_TRACKED_MODELS"):
            signals.connect_tracked_signals()
        [issue] = check_tracked_models_setting()
        assert _e028("REBAC_TRACKED_MODELS") == []
    assert issue.id == "rebac.E018"
    assert "comma" in issue.hint


def test_tracked_models_without_the_comma_say_so():
    with override_settings(REBAC_TRACKED_MODELS="testapp.BackingStage"):
        with pytest.raises(ImproperlyConfigured, match=r'needs its comma: \("app_label.Model",\)'):
            _ = app_settings.REBAC_TRACKED_MODELS


@pytest.mark.parametrize(
    "value", [["testapp.BackingStage"], ("testapp.BackingStage",)], ids=["list", "tuple"]
)
def test_a_list_or_a_tuple_of_labels_gates_the_model_at_run_time(value):
    from django.db.models.signals import pre_save

    from rebac import signals
    from tests.testapp.models import BackingStage

    with override_settings(REBAC_TRACKED_MODELS=[]):
        assert not pre_save.has_listeners(BackingStage)
    with override_settings(REBAC_TRACKED_MODELS=value):
        assert signals.tracked_model(BackingStage) is True
        assert pre_save.has_listeners(BackingStage)
        assert check_tracked_models_setting() == []


def test_ready_stops_on_tracked_models_it_does_not_accept():
    from django.apps import apps

    with override_settings(REBAC_TRACKED_MODELS="testapp.BackingStage"):
        with pytest.raises(ImproperlyConfigured, match="REBAC_TRACKED_MODELS"):
            apps.get_app_config("rebac").ready()
    apps.get_app_config("rebac").ready()


# ---------- Settings their readers refuse ----------


@pytest.mark.parametrize("value", [None, b"local", "Local", ["local"], 5])
def test_any_other_backend_is_refused_by_backend(value):
    from rebac.backends import reset_backend

    reset_backend()
    with override_settings(REBAC_BACKEND=value):
        with pytest.raises(ValueError, match="REBAC_BACKEND"):
            rebac.backend()
        assert [i.id for i in check_backend_setting()][:1] == ["rebac.E001"]
        assert _e028("REBAC_BACKEND") == []
    reset_backend()


@pytest.mark.django_db
@pytest.mark.parametrize("value", [None, b"redact", "Redact", 3])
def test_any_other_field_read_mode_is_refused_by_the_field_gates(value):
    from rebac.field_visibility import effective_field_deny_mode
    from tests.testapp.models import Post

    with override_settings(REBAC_FIELD_READ_MODE=value):
        with pytest.raises(ValueError, match="field read deny mode"):
            effective_field_deny_mode()
        with pytest.raises(ValueError, match="field read deny mode"):
            list(Post.objects.with_actor(rebac.SubjectRef.of("auth/user", "a")))
        assert _e028("REBAC_FIELD_READ_MODE") == []


@pytest.mark.parametrize("value", [None, b"header", "Header", ["header"]])
def test_any_other_zookie_transport_transports_nothing(value):
    from rebac.middleware import ActorMiddleware

    request = RequestFactory().get("/", HTTP_X_REBAC_ZOOKIE="local.123")
    middleware = ActorMiddleware(lambda request: None)
    with override_settings(REBAC_ZOOKIE_TRANSPORT="header"):
        assert middleware._rehydrate_zookie(request) is not None
    with override_settings(REBAC_ZOOKIE_TRANSPORT=value):
        assert middleware._rehydrate_zookie(request) is None
        assert _e028("REBAC_ZOOKIE_TRANSPORT") == []


# ---------- Checks that cannot run ----------


def test_a_check_that_cannot_run_says_so_under_each_of_its_ids():
    with override_settings(REBAC_UNIVERSAL_ADMIN_ROLE=5):
        reported = check_universal_admin_in_roles()
    assert [(issue.id, issue.level) for issue in reported] == [
        ("rebac.E005", checks.ERROR),
        ("rebac.W004", checks.WARNING),
    ]
    assert all(
        "check_universal_admin_in_roles did not run" in issue.msg
        and "REBAC_UNIVERSAL_ADMIN_ROLE is int 5" in issue.msg
        for issue in reported
    )


def test_silencing_e028_does_not_hide_a_check_that_could_not_run():
    with override_settings(REBAC_UNIVERSAL_ADMIN_ROLE=5, SILENCED_SYSTEM_CHECKS=["rebac.E028"]):
        reported = checks.run_checks(tags=["rebac"])
        shown = [issue for issue in reported if not issue.is_silenced()]
    assert {issue.id for issue in shown if "did not run" in issue.msg} == {
        "rebac.E005",
        "rebac.W004",
    }
    assert not [issue for issue in shown if issue.id == "rebac.E028"]


@pytest.mark.django_db
def test_a_database_check_that_cannot_run_fails_a_database_run():
    """``check --database default --tag database`` selects it by its tags and reports the skip."""
    with override_settings(REBAC_RESOURCE_ID_ATTR=None):
        reported = checks.run_checks(tags=["database"], databases=["default"])
    assert {issue.id for issue in reported} == {
        "rebac.E014",
        "rebac.E015",
        "rebac.E016",
        "rebac.E018",
        "rebac.W014",
    }
    assert all("check_policy_models did not run" in issue.msg for issue in reported)


def test_every_rebac_check_is_registered_with_its_ids_and_tags():
    registered = {
        check.__name__: set(check.tags)
        for check in checks.registry.registry.get_checks(include_deployment_checks=True)
        if "rebac" in getattr(check, "tags", ())
    }
    assert set(registered) == set(_CHECK_IDS)
    assert registered["check_policy_models"] == {"rebac", "database"}
    assert all(
        tags == {"rebac"} for name, tags in registered.items() if name != "check_policy_models"
    )


# ---------- Logged once where no check runs ----------


def test_ready_logs_each_setting_it_does_not_accept(caplog):
    from rebac.apps import log_wrongly_typed_settings

    with override_settings(
        REBAC_SUPERUSER_BYPASS="True", REBAC_ACTOR_PROFILES=1, REBAC_DEPTH_LIMIT="8"
    ):
        with caplog.at_level(logging.WARNING, logger="rebac"):
            log_wrongly_typed_settings()
    records = {record.getMessage(): record.levelno for record in caplog.records}
    assert records == {
        "REBAC_ACTOR_PROFILES is int 1, not True or False: it is read as False (rebac.E028).": (
            logging.WARNING
        ),
        "REBAC_DEPTH_LIMIT is str '8', not an int: every read of it raises "
        "ImproperlyConfigured (rebac.E028).": logging.ERROR,
        "REBAC_SUPERUSER_BYPASS is str 'True', not True or False: it is read as False "
        "(rebac.E028).": logging.WARNING,
    }


def test_ready_logs_the_settings(monkeypatch):
    from django.apps import apps

    from rebac import apps as rebac_apps

    calls = []
    monkeypatch.setattr(rebac_apps, "log_wrongly_typed_settings", lambda: calls.append(1))
    apps.get_app_config("rebac").ready()
    assert calls == [1]


# ---------- No module reads a setting around the accessor ----------


def test_no_module_reads_a_rebac_setting_but_through_app_settings():
    """Every ``REBAC_*`` read goes through ``app_settings``, which knows its type."""
    package = Path(rebac.__file__).parent
    allowed = {"app_settings", "conf_app_settings", "_app_settings"}
    found = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "conf.py" and path.parent == package:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith("REBAC_"):
                base = node.value
                if not (isinstance(base, ast.Name) and base.id in allowed):
                    found.append(f"{path.relative_to(package)}:{node.lineno} .{node.attr}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"getattr", "hasattr", "setattr"}
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and str(node.args[1].value).startswith("REBAC_")
            ):
                found.append(f"{path.relative_to(package)}:{node.lineno} {node.func.id}()")
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                if str(node.slice.value).startswith("REBAC_"):
                    found.append(f"{path.relative_to(package)}:{node.lineno} [...]")
    assert found == []


# ---------- What the closed reading does ----------


@pytest.mark.parametrize("value", ["False", "True", 1])
def test_sudo_is_refused_while_allow_sudo_is_not_a_bool(value):
    with override_settings(REBAC_ALLOW_SUDO=value):
        with pytest.raises(SudoNotAllowedError):
            with sudo(reason="probe"):
                pass


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["False", "True", 1])
def test_superusers_are_not_bypassed_while_the_bypass_is_not_a_bool(value):
    from rebac.actors import to_subject_ref
    from rebac.middleware import ActorMiddleware

    user = get_user_model().objects.create(username="root", is_superuser=True, is_active=True)
    request = RequestFactory().get("/")
    request.user = user
    middleware = ActorMiddleware(lambda request: None)
    with override_settings(REBAC_SUPERUSER_BYPASS=True, REBAC_ALLOW_SUDO=True):
        assert RebacBackend().has_perm(user, "nothing.here") is True
        assert middleware._should_sudo(request, to_subject_ref(user)) is True
    with override_settings(REBAC_SUPERUSER_BYPASS=value):
        assert RebacBackend().has_perm(user, "nothing.here") is False
        assert RebacBackend().has_module_perms(user, "nothing") is False
        assert middleware._should_sudo(request, to_subject_ref(user)) is False


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["True", 1])
def test_the_superuser_bypass_does_not_open_sudo_while_allow_sudo_is_not_a_bool(value):
    from rebac.actors import to_subject_ref
    from rebac.middleware import ActorMiddleware

    user = get_user_model().objects.create(username="root", is_superuser=True, is_active=True)
    request = RequestFactory().get("/")
    request.user = user
    with override_settings(REBAC_SUPERUSER_BYPASS=True, REBAC_ALLOW_SUDO=value):
        assert (
            ActorMiddleware(lambda request: None)._should_sudo(request, to_subject_ref(user))
            is False
        )


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["False", "True", 0, None])
def test_enforcement_stays_on_and_is_reported_while_enabled_is_not_a_bool(value):
    from rebac.actors import enforcing

    user = get_user_model().objects.create(username="plain", is_active=True)
    with override_settings(REBAC_ENABLED=False):
        assert enforcing() is False
        assert RebacBackend().has_perm(user, "nothing.here") is True
        assert [issue.id for issue in check_permissions_enforced()] == ["rebac.W011"]
    with override_settings(REBAC_ENABLED=value):
        assert enforcing() is True
        assert RebacBackend().has_perm(user, "nothing.here") is False
        # W011 says enforcement is off; it is not, and E028 says why the setting is ignored.
        assert check_permissions_enforced() == []
        [issue] = _e028("REBAC_ENABLED")
        assert issue.msg.endswith("it is read as True.")


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["False", 0, None])
def test_a_queryset_without_an_actor_raises_while_strict_mode_is_not_a_bool(value):
    from tests.testapp.models import Post

    with override_settings(REBAC_STRICT_MODE=value):
        with pytest.raises(MissingActorError):
            list(Post.objects.all())


@pytest.mark.parametrize("value", ["False", 0, None])
def test_sudo_needs_a_reason_while_require_reason_is_not_a_bool(value):
    with override_settings(REBAC_REQUIRE_SUDO_REASON=value):
        with pytest.raises(SudoReasonRequiredError):
            with sudo():
                pass


@pytest.mark.parametrize("value", ["True", "prove ", 1])
def test_the_switches_read_off_while_not_spelled_as_declared(value):
    from rebac import display, rowscope, walk

    with override_settings(
        REBAC_ROW_SCOPE=value, REBAC_DISPLAY_FIELDS=value, REBAC_BOUNDED_WALK=value
    ):
        assert (rowscope.mode(), display.mode(), walk.mode()) == ("", "", "")


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["True", 1])
def test_actor_profiles_read_off_while_not_a_bool(local, value):
    from rebac import evaluator_scope
    from rebac.compile.read import _actor_profile
    from tests.mechanism_policies import MEMBER, grant

    local.write_relationships([grant("scope/thread", 1, "reader")])
    with evaluator_scope():
        with override_settings(REBAC_ACTOR_PROFILES=True):
            assert _actor_profile(local, MEMBER, "default") is not None
        with override_settings(REBAC_ACTOR_PROFILES=value):
            assert _actor_profile(local, MEMBER, "default", fresh=True) is None


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["True", 1])
def test_children_keep_their_statement_while_structural_prefetch_is_not_a_bool(
    rows, settings, value
):
    from rebac import structural
    from rebac.testing import install_schema
    from tests.test_structural_prefetch import SCHEMA, grant, threads_with_messages

    install_schema(SCHEMA).write_relationships([grant("scope/thread", 1, "reader")])
    settings.REBAC_STRUCTURAL_PREFETCH = value
    assert structural.enabled() is False
    found, statements = threads_with_messages()
    assert found == {1: [1, 2]}
    child = next(sql for sql in statements if 'FROM "testapp_scopemessage"' in sql)
    assert "rebac_" in child


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["True", 1])
def test_stored_ids_keep_their_guard_while_trusted_ids_is_not_a_bool(local, settings, value):
    from rebac.compile import read
    from tests.mechanism_policies import grant
    from tests.test_trusted_ids import GUARD
    from tests.test_trusted_ids import rows as read_rows
    from tests.testapp.models import ScopePart

    local.write_relationships([grant("scope/thread", 1, "reader")])
    settings.REBAC_TRUSTED_IDS = value
    read.reset()
    try:
        found, statements = read_rows(ScopePart)
        assert found == [1, 2]
        assert any(GUARD in sql for sql in statements)
    finally:
        settings.REBAC_TRUSTED_IDS = False
        read.reset()


# ---------- The readers of a closed reading whose raw value would be falsy ----------
# 0, None and "" are falsy: a reader that bypassed the accessor would read them as off.

FALSY_WRONG = [0, None, ""]


@pytest.mark.parametrize("value", FALSY_WRONG)
def test_an_observer_without_an_actor_stays_scoped_while_strict_mode_is_not_a_bool(value):
    """The non-strict path (a tracked delete's observer, a related read) answers scoped."""
    from rebac.actors import resolve

    with override_settings(REBAC_STRICT_MODE=False):
        assert resolve(mode="ambient", strict=False, subject_label="probe") == (None, True)
    with override_settings(REBAC_STRICT_MODE=value):
        assert resolve(mode="ambient", strict=False, subject_label="probe") == (None, False)
        with pytest.raises(MissingActorError):
            resolve(mode="ambient", strict=True, subject_label="probe")


@pytest.mark.django_db
@pytest.mark.parametrize("value", FALSY_WRONG)
def test_a_denied_save_is_audited_while_audit_denials_is_not_a_bool(value):
    from rebac import PermissionDenied, SubjectRef
    from rebac.backends import reset_backend
    from rebac.models import PermissionAuditEvent
    from rebac.testing import install_schema
    from tests.testapp.models import Post

    reset_backend()
    install_schema(
        """
        definition auth/user {}
        definition blog/post {
            relation owner: auth/user
            permission read = owner
            permission write = owner
            permission delete = owner
            permission create = owner
        }
        """
    )
    with sudo(reason="fixture"):
        post = Post.objects.create(title="hi")
    PermissionAuditEvent.objects.all().delete()
    with override_settings(REBAC_AUDIT_DENIALS=value):
        post._rebac_actor = SubjectRef.of("auth/user", "bob")
        post.title = "rename"
        with pytest.raises(PermissionDenied):
            post.save()
    assert PermissionAuditEvent.objects.filter(reason__startswith="denied:").count() == 1
    reset_backend()


@pytest.mark.parametrize("value", FALSY_WRONG)
def test_the_bare_prefetch_lint_runs_while_its_switch_is_not_a_bool(value):
    from rebac.checks import check_cross_rbac_relations

    with override_settings(REBAC_LINT_BARE_PREFETCH=False):
        assert check_cross_rbac_relations() == []
    with override_settings(REBAC_LINT_BARE_PREFETCH=value):
        assert {issue.id for issue in check_cross_rbac_relations()} == {"rebac.W003"}


@pytest.mark.parametrize("value", FALSY_WRONG)
def test_a_conditional_field_stays_hidden_while_its_switch_is_not_a_bool(value):
    from rebac.field_visibility import _conditional_field_is_visible

    class Unasked:
        def __getattr__(self, name):
            raise AssertionError("a closed reading asks nothing")

    with override_settings(REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL=value):
        assert (
            _conditional_field_is_visible(
                Unasked(),
                actor=rebac.SubjectRef.of("auth/user", "a"),
                action="read__title",
                resource=rebac.ObjectRef("blog/post", "1"),
            )
            is False
        )


@pytest.mark.parametrize("value", FALSY_WRONG)
def test_the_switches_read_off_for_a_falsy_value_too(value):
    from rebac import codec, display, rowscope, structural, walk

    with override_settings(
        REBAC_ROW_SCOPE=value,
        REBAC_DISPLAY_FIELDS=value,
        REBAC_BOUNDED_WALK=value,
        REBAC_STRUCTURAL_PREFETCH=value,
        REBAC_TRUSTED_IDS=value,
    ):
        assert (rowscope.mode(), display.mode(), walk.mode()) == ("", "", "")
        assert structural.enabled() is False
        assert codec._trusted(type("Codec", (), {"kind": "integer"})()) is False


# ---------- An authenticated user with no id, served as anonymous, is logged ----------


@pytest.mark.django_db
def test_an_authenticated_user_served_as_anonymous_is_logged_once_without_its_values(
    settings, caplog
):
    from rebac.actors import _served_as_anonymous, anonymous_actor, default_resolver

    settings.REBAC_USER_ID_ATTR = "first_name"
    _served_as_anonymous.clear()
    users = get_user_model().objects
    first = users.create(username="first-secret", first_name="", email="first@example.com")
    second = users.create(username="second-secret", first_name="", email="second@example.com")
    with caplog.at_level(logging.DEBUG, logger="rebac"):
        for user in (first, second, first):
            request = RequestFactory().get("/")
            request.user = user
            assert default_resolver(request) == anonymous_actor()
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "An authenticated auth.User is served as the anonymous actor: User instance has no "
        "first_name; save it before using it as a subject."
    ]
    assert caplog.records[0].levelno == logging.WARNING
    assert not any("secret" in message or "example.com" in message for message in messages)


# ---------- Exactly the values declared ----------


class _StrLayout(str, __import__("enum").Enum):
    DENORMALIZED = "denormalized"
    REGISTRY = "registry"


class _PlainLayout(__import__("enum").Enum):
    DENORMALIZED = "denormalized"


@pytest.mark.parametrize(
    ("value", "model"),
    [
        (_StrLayout.REGISTRY, "RelationshipRegistry"),
        (_StrLayout.DENORMALIZED, "Relationship"),
        (__import__("enum").StrEnum("Layout", {"R": "registry"}).R, "RelationshipRegistry"),
    ],
    ids=["str-enum-registry", "str-enum-denormalized", "strenum-registry"],
)
def test_a_str_enum_layout_is_read_as_its_value(value, model):
    from rebac.models import active_relationship_model

    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=value):
        assert active_relationship_model().__name__ == model
        assert check_backend_setting() == [] or {i.id for i in check_backend_setting()} == {
            "rebac.W005"
        }


@pytest.mark.parametrize(
    "value",
    [
        "denormalized ",
        _PlainLayout.DENORMALIZED,
        __import__("django.utils.functional").utils.functional.lazystr("denormalized"),
    ],
    ids=["padded", "plain-enum", "lazy"],
)
def test_a_layout_that_only_looks_like_one_raises(value):
    from rebac.models import active_relationship_model

    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=value):
        with pytest.raises(ImproperlyConfigured, match="REBAC_LOCAL_BACKEND_STORAGE"):
            active_relationship_model()
        assert [i.id for i in check_backend_setting()] == ["rebac.E006"]


@pytest.mark.parametrize(
    ("value", "item"),
    [
        (["testapp.BackingStage", None], "(item 1 is NoneType None)"),
        (
            [
                __import__("django.utils.functional").utils.functional.lazystr(
                    "testapp.BackingStage"
                )
            ],
            "(item 0 is __proxy__ 'testapp.BackingStage')",
        ),
        (("testapp.BackingStage", 7), "(item 1 is int 7)"),
    ],
    ids=["none-item", "lazy-item", "int-item"],
)
def test_tracked_models_name_the_item_that_is_not_a_str(value, item):
    """0.37 tracked the str items of such a list and left the other item's model ungated."""
    from rebac import signals
    from tests.testapp.models import BackingStage

    with override_settings(REBAC_TRACKED_MODELS=value):
        with pytest.raises(ImproperlyConfigured) as raised:
            signals._register_model(BackingStage)
        [issue] = check_tracked_models_setting()
    assert item in str(raised.value)
    assert item in issue.msg


@pytest.mark.django_db
def test_rows_loaded_while_the_resource_id_setting_has_no_safe_reading_raise():
    """``from_db`` raises when the setting that names a row's identity has no safe reading."""
    from tests.testapp.models import Post

    with sudo(reason="fixture"):
        Post._base_manager.create(title="t")
    with override_settings(REBAC_RESOURCE_ID_ATTR=None):
        with sudo(reason="probe"), pytest.raises(ImproperlyConfigured, match="RESOURCE_ID_ATTR"):
            list(Post.objects.all())
