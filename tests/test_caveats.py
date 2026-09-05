"""Caveat evaluation tests — tri-state plumbed through LocalBackend.

The schema:

    caveat link_not_expired(expires_at timestamp, now timestamp) {
        now < expires_at
    }

A row of `viewer @ auth/user:u1 with link_not_expired` carries
`expires_at` as static context (pinned at write time); the caller supplies
`now` at check time. Three states:

  - `now < expires_at`  → HAS
  - `now >= expires_at` → NO
  - `now` not supplied  → CONDITIONAL(missing=("now",))

When `cel-python` is missing, evaluating any caveat raises
`CaveatUnsupportedError` with an install hint.

`accessible()` is read-side conservative: CONDITIONAL and False rows are
silently excluded.
"""

from __future__ import annotations

import datetime
import sys

import pytest

from rebac import (
    CaveatUnsupportedError,
    LocalBackend,
    ObjectRef,
    PermissionResult,
    RelationshipTuple,
    SubjectRef,
)
from rebac.schema import parse_zed

SCHEMA_TEXT = """
caveat link_not_expired(expires_at timestamp, now timestamp) {
    now < expires_at
}

definition auth/user {}

definition blog/post {
    relation viewer: auth/user | auth/user with link_not_expired
    permission read = viewer
}
"""


@pytest.fixture
def backend(db):
    # Reset caveat compile cache between tests so the cel-python-missing test
    # doesn't accidentally hit a previously-compiled program.
    from rebac.caveats import reset_cache

    reset_cache()
    b = LocalBackend()
    b.set_schema(parse_zed(SCHEMA_TEXT))
    return b


def _user(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/user", id_)


def _post(id_: str) -> ObjectRef:
    return ObjectRef("blog/post", id_)


# ISO 8601 strings — that's what JSONField round-trips for caveat_context.
FUTURE = "2099-01-01T00:00:00Z"
PAST = "1999-01-01T00:00:00Z"
EXPIRED = "2000-01-01T00:00:00Z"


def _write_caveated_viewer(backend, post_id: str, user_id: str, expires_at: str) -> None:
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post(post_id),
                relation="viewer",
                subject=_user(user_id),
                caveat_name="link_not_expired",
                caveat_context={"expires_at": expires_at},
            ),
        ]
    )


def test_check_access_caveat_satisfied_returns_has(backend):
    _write_caveated_viewer(backend, "p1", "u1", expires_at=FUTURE)
    result = backend.check_access(
        subject=_user("u1"),
        action="read",
        resource=_post("p1"),
        context={"now": PAST},
    )
    assert result.allowed is True
    assert result.result == PermissionResult.HAS_PERMISSION
    assert result.conditional_on == ()


def test_check_access_caveat_denies_returns_no(backend):
    _write_caveated_viewer(backend, "p2", "u2", expires_at=EXPIRED)
    # `now` is after the link's expiration -> caveat returns False.
    result = backend.check_access(
        subject=_user("u2"),
        action="read",
        resource=_post("p2"),
        context={"now": FUTURE},
    )
    assert result.allowed is False
    assert result.result == PermissionResult.NO_PERMISSION
    assert result.conditional_on == ()


def test_check_access_missing_param_returns_conditional(backend):
    _write_caveated_viewer(backend, "p3", "u3", expires_at=FUTURE)
    # Caller supplied no `now` — the row's static context has `expires_at`,
    # so only `now` is missing.
    result = backend.check_access(
        subject=_user("u3"),
        action="read",
        resource=_post("p3"),
        context={},
    )
    assert result.allowed is False
    assert result.result == PermissionResult.CONDITIONAL_PERMISSION
    assert result.conditional_on == ("now",)


def test_check_access_missing_param_no_context_arg(backend):
    """No context arg at all — same outcome as empty context."""
    _write_caveated_viewer(backend, "p4", "u4", expires_at=FUTURE)
    result = backend.check_access(
        subject=_user("u4"),
        action="read",
        resource=_post("p4"),
    )
    assert result.result == PermissionResult.CONDITIONAL_PERMISSION
    assert result.conditional_on == ("now",)


def test_check_access_dynamic_context_overrides_static(backend):
    """Dynamic context wins over the row's pinned static context."""
    # Row says `expires_at = FUTURE` but caller overrides with EXPIRED.
    _write_caveated_viewer(backend, "p5", "u5", expires_at=FUTURE)
    result = backend.check_access(
        subject=_user("u5"),
        action="read",
        resource=_post("p5"),
        context={"now": PAST, "expires_at": EXPIRED},
    )
    # PAST < EXPIRED → True
    assert result.allowed is True


def test_accessible_excludes_conditional_when_param_missing(backend):
    """Without `now`, all rows are CONDITIONAL → accessible() returns empty."""
    _write_caveated_viewer(backend, "p_a", "u", expires_at=FUTURE)
    _write_caveated_viewer(backend, "p_b", "u", expires_at=EXPIRED)

    # No `now` → all rows are CONDITIONAL → accessible() silently excludes.
    ids = set(
        backend.accessible(
            subject=_user("u"),
            action="read",
            resource_type="blog/post",
            context={},
        )
    )
    assert ids == set()


def test_accessible_excludes_only_false_rows(backend):
    """With `now` supplied, only the truly-denying row is excluded."""
    _write_caveated_viewer(backend, "p_ok", "u", expires_at=FUTURE)
    _write_caveated_viewer(backend, "p_expired", "u", expires_at=EXPIRED)

    ids = set(
        backend.accessible(
            subject=_user("u"),
            action="read",
            resource_type="blog/post",
            context={"now": "2050-01-01T00:00:00Z"},  # after p_expired, before p_ok
        )
    )
    assert ids == {"p_ok"}


def test_uncaveated_row_unaffected(backend):
    """Rows without a caveat name continue to evaluate as before."""
    # The schema explicitly permits both plain and caveated viewer tuples.
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post("p_plain"),
                relation="viewer",
                subject=_user("u_plain"),
            ),
        ]
    )
    result = backend.check_access(
        subject=_user("u_plain"),
        action="read",
        resource=_post("p_plain"),
    )
    assert result.result == PermissionResult.HAS_PERMISSION


def test_unconditional_row_wins_over_conditional(backend):
    """If any path is unconditionally allowed, return HAS even if other paths
    would be CONDITIONAL.
    """
    # Two viewer rows on the same post — one caveated (conditional without
    # `now`), one plain.
    _write_caveated_viewer(backend, "p_mixed", "u", expires_at=FUTURE)
    backend.write_relationships(
        [
            RelationshipTuple(
                resource=_post("p_mixed"),
                relation="viewer",
                subject=_user("u"),
            ),
        ]
    )
    # No `now` → caveated row would be CONDITIONAL; the plain row is HAS.
    # The plain row wins.
    result = backend.check_access(
        subject=_user("u"),
        action="read",
        resource=_post("p_mixed"),
    )
    assert result.result == PermissionResult.HAS_PERMISSION


def test_evaluate_function_returns_tri_state():
    """Direct unit test of `caveats.evaluate()`."""
    from rebac.caveats import evaluate, reset_cache
    from rebac.schema.ast import Caveat, CaveatParam

    reset_cache()
    caveat = Caveat(
        name="link_not_expired",
        params=(
            CaveatParam("expires_at", "timestamp"),
            CaveatParam("now", "timestamp"),
        ),
        expression="now < expires_at",
    )

    # Both supplied, satisfied.
    verdict, missing = evaluate(caveat, {"expires_at": FUTURE}, {"now": PAST})
    assert verdict is True and missing == ()

    # Both supplied, denies.
    verdict, missing = evaluate(caveat, {"expires_at": EXPIRED}, {"now": FUTURE})
    assert verdict is False and missing == ()

    # `now` missing.
    verdict, missing = evaluate(caveat, {"expires_at": FUTURE}, {})
    assert verdict is None and missing == ("now",)

    # Both missing.
    verdict, missing = evaluate(caveat, {}, {})
    assert verdict is None and missing == ("expires_at", "now")


def test_evaluate_handles_datetime_objects():
    """Python datetime values (not just ISO strings) work too."""
    from rebac.caveats import evaluate, reset_cache
    from rebac.schema.ast import Caveat, CaveatParam

    reset_cache()
    caveat = Caveat(
        name="link_not_expired",
        params=(
            CaveatParam("expires_at", "timestamp"),
            CaveatParam("now", "timestamp"),
        ),
        expression="now < expires_at",
    )
    now = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    expires = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
    verdict, _ = evaluate(caveat, {"expires_at": expires}, {"now": now})
    assert verdict is True


def test_static_overrides_dynamic_in_evaluate():
    """Request context cannot replace stored policy constraints."""
    from rebac.caveats import evaluate, reset_cache
    from rebac.schema.ast import Caveat, CaveatParam

    reset_cache()
    caveat = Caveat(
        name="link_not_expired",
        params=(
            CaveatParam("expires_at", "timestamp"),
            CaveatParam("now", "timestamp"),
        ),
        expression="now < expires_at",
    )
    # Static says future, and conflicting request values cannot replace it.
    verdict, _ = evaluate(
        caveat,
        {"expires_at": FUTURE, "now": PAST},
        {"expires_at": EXPIRED, "now": PAST},
    )
    assert verdict is True

    verdict, _ = evaluate(
        caveat,
        {"expires_at": FUTURE, "now": PAST},
        {"expires_at": PAST},
    )
    assert verdict is True


def test_compile_cache_keyed_by_name_and_hash():
    """Same name + body → cached. Body change → new entry."""
    from rebac.caveats import _compile_cache, compile_caveat, reset_cache
    from rebac.schema.ast import Caveat, CaveatParam

    reset_cache()
    c1 = Caveat("c", (CaveatParam("x", "int"),), "x > 0")
    p1a = compile_caveat(c1)
    p1b = compile_caveat(c1)
    assert p1a is p1b
    assert len(_compile_cache) == 1

    c2 = Caveat("c", (CaveatParam("x", "int"),), "x < 0")  # same name, different body
    p2 = compile_caveat(c2)
    assert p2 is not p1a
    assert len(_compile_cache) == 2


@pytest.mark.parametrize("value", ["false", "true", "0", 1, [False], {"enabled": False}])
def test_boolean_caveat_rejects_truthy_non_boolean_values(value):
    from rebac.caveats import evaluate
    from rebac.schema.ast import Caveat, CaveatParam

    caveat = Caveat("flag", (CaveatParam("enabled", "bool"),), "enabled")
    with pytest.raises(CaveatUnsupportedError, match="boolean"):
        evaluate(caveat, {}, {"enabled": value})


def test_caveat_result_must_be_a_boolean():
    from rebac.caveats import evaluate
    from rebac.schema.ast import Caveat, CaveatParam

    caveat = Caveat("value", (CaveatParam("count", "int"),), "count")
    with pytest.raises(CaveatUnsupportedError, match="boolean"):
        evaluate(caveat, {}, {"count": 1})


def test_caveat_unsupported_when_celpy_missing(monkeypatch):
    """Schema with a caveat but cel-python unimportable → CaveatUnsupportedError.

    We simulate the missing dep by setting `sys.modules['celpy'] = None`,
    which makes `import celpy` raise ImportError.
    """
    from rebac import caveats as caveats_mod

    # Reset module state so the next _load_celpy() retries the import.
    caveats_mod._CELPY_TRIED = False
    caveats_mod._CELPY_MODULE = None
    caveats_mod.reset_cache()

    monkeypatch.setitem(sys.modules, "celpy", None)

    from rebac.schema.ast import Caveat, CaveatParam

    caveat = Caveat("c", (CaveatParam("x", "int"),), "x > 0")

    with pytest.raises(CaveatUnsupportedError) as excinfo:
        caveats_mod.evaluate(caveat, {}, {"x": 1})
    assert "django-zed-rebac[caveats]" in str(excinfo.value)

    # Reset state for the next tests in the suite.
    caveats_mod._CELPY_TRIED = False
    caveats_mod._CELPY_MODULE = None


def test_unknown_caveat_in_row_is_treated_as_deny(backend):
    """Row references a caveat the schema doesn't know — fail closed."""
    from rebac.models import Relationship

    # Simulate a stale row after schema removal; public writes reject it.
    Relationship.objects.create(
        resource_type="blog/post",
        resource_id="p_unknown",
        relation="viewer",
        subject_type="auth/user",
        subject_id="u_unknown",
        caveat_name="does_not_exist",
    )
    result = backend.check_access(
        subject=_user("u_unknown"),
        action="read",
        resource=_post("p_unknown"),
        context={"now": PAST},
    )
    # Unknown caveat → row is silently treated as absent.
    assert result.result == PermissionResult.NO_PERMISSION
