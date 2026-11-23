"""Settings are read by their declared type: a wrongly typed value never loosens anything.

A deployment that passes settings through an environment variable gets strings, and
``"False"`` is truthy. Each setting either reads such a value as its closed reading (a
protection on, a loosening off, a mechanism that skips or shortens a permission
statement off) or raises ``ImproperlyConfigured`` where no reading is safe, and
``rebac.E028`` reports it by name (ARCHITECTURE.md § Settings catalog).

The closed readings are written out below, not read from ``rebac.conf``: the table is
the specification the module is held to.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, override_settings

from rebac import sudo
from rebac.backends.auth import RebacBackend
from rebac.checks import (
    check_permissions_enforced,
    check_setting_types,
    check_tracked_models_setting,
    check_universal_admin_in_roles,
)
from rebac.conf import _DEFAULTS, _TYPES, app_settings
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
# No safe reading: a number, a name, a dotted path, a type. Reading it raises.
RAISES = {
    "REBAC_DEPTH_LIMIT": "an int",
    "REBAC_EVALUATOR_CACHE_SIZE": "an int",
    "REBAC_LOCAL_BACKEND_REGISTRY_BATCH_SIZE": "an int",
    "REBAC_GC_INTERVAL_SECONDS": "an int",
    "REBAC_LOOKUP_CACHE_TTL": "an int",
    "REBAC_RELATIONSHIP_MODEL": "a str",
    "REBAC_DEFAULT_CONSISTENCY": "a str",
    "REBAC_CACHE_ALIAS": "a str",
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
# Reported by a check of their own, which refuses any value outside a fixed set.
OWN_CHECK = {
    "REBAC_BACKEND": "rebac.E001",
    "REBAC_LOCAL_BACKEND_STORAGE": "rebac.E006",
    "REBAC_ZOOKIE_TRANSPORT": "rebac.E007",
    "REBAC_FIELD_READ_MODE": "rebac.E008",
    "REBAC_TRACKED_MODELS": "rebac.E018",
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


def _closed_cases():
    for name, closed in sorted(CLOSED.items()):
        for value in SWITCH_WRONG if name in SWITCHES else BOOL_WRONG:
            yield pytest.param(name, value, closed, id=f"{name}={value!r}")


def _raising_cases():
    for name, expected in sorted(RAISES.items()):
        for value in WRONG[expected]:
            yield pytest.param(name, value, expected, id=f"{name}={value!r}")


def _e028(name):
    return [issue for issue in check_setting_types() if issue.msg.startswith(f"{name} is ")]


def test_every_setting_is_classified():
    assert set(CLOSED) | set(RAISES) | set(OWN_CHECK) == set(_DEFAULTS) == set(_TYPES)
    assert not (set(CLOSED) & set(RAISES))


def test_the_defaults_and_the_test_settings_are_well_typed():
    assert check_setting_types() == []
    for name, default in _DEFAULTS.items():
        assert _TYPES[name].checked_by or _TYPES[name].accepts(default), name


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
    ],
)
def test_a_value_of_the_declared_type_is_read_as_written(name, value):
    with override_settings(**{name: value}):
        assert getattr(app_settings, name) == value
        assert _e028(name) == []


@pytest.mark.parametrize("name", sorted(OWN_CHECK))
def test_a_setting_with_its_own_check_is_left_to_it(name):
    with override_settings(**{name: 7}):
        assert getattr(app_settings, name) == 7
        assert _e028(name) == []


def test_tracked_models_may_be_a_tuple():
    with override_settings(REBAC_TRACKED_MODELS=("testapp.BackingStage",)):
        assert check_tracked_models_setting() == []


def test_a_check_that_reads_a_refused_setting_reports_nothing_and_e028_reports_it():
    with override_settings(REBAC_UNIVERSAL_ADMIN_ROLE=5):
        assert check_universal_admin_in_roles() == []
        reported = checks.run_checks(tags=["rebac"])
    assert [issue.msg.split(" ")[0] for issue in reported if issue.id == "rebac.E028"] == [
        "REBAC_UNIVERSAL_ADMIN_ROLE"
    ]


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
