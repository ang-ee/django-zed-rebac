"""Tests for ``rebac.consistency`` Zookie ContextVar + freshness resolution."""

from __future__ import annotations

import pytest

from rebac import (
    Consistency,
    Zookie,
    current_zookie,
    record_zookie,
    zookie_scope,
)
from rebac.consistency import effective_consistency

# ---------- ContextVar machinery ----------


def test_current_zookie_is_none_outside_scope():
    assert current_zookie() is None


def test_zookie_scope_isolates_writes():
    with zookie_scope():
        assert current_zookie() is None
        record_zookie(Zookie("local", "100"))
        assert current_zookie() == Zookie("local", "100")
    # Scope torn down — back to ambient None.
    assert current_zookie() is None


def test_zookie_scope_accepts_initial():
    initial = Zookie("local", "42")
    with zookie_scope(initial=initial):
        assert current_zookie() == initial


def test_nested_scopes_dont_leak():
    with zookie_scope():
        record_zookie(Zookie("local", "outer"))
        with zookie_scope():
            assert current_zookie() is None
            record_zookie(Zookie("local", "inner"))
            assert current_zookie() == Zookie("local", "inner")
        assert current_zookie() == Zookie("local", "outer")


def test_record_zookie_none_is_noop():
    with zookie_scope(initial=Zookie("local", "42")):
        record_zookie(None)
        # The existing token is preserved when None is recorded.
        assert current_zookie() == Zookie("local", "42")


# ---------- effective_consistency ----------


def test_no_scope_returns_pair_of_none():
    """No scope, no explicit args → backend default applies."""
    consistency, zookie = effective_consistency()
    assert consistency is None
    assert zookie is None


def test_explicit_consistency_wins():
    with zookie_scope(initial=Zookie("local", "100")):
        consistency, zookie = effective_consistency(
            explicit_consistency=Consistency.MINIMIZE_LATENCY
        )
        # Caller asked explicitly for stale-tolerant reads — honour it.
        assert consistency == Consistency.MINIMIZE_LATENCY
        assert zookie is None


def test_explicit_zookie_wins():
    with zookie_scope(initial=Zookie("local", "100")):
        token = Zookie("local", "999")
        _consistency, zookie = effective_consistency(explicit_zookie=token)
        assert zookie == token


def test_auto_upgrades_to_at_least_as_fresh_with_in_scope_zookie():
    """The happy path: post-write read auto-upgrades."""
    with zookie_scope():
        record_zookie(Zookie("local", "500"))
        consistency, zookie = effective_consistency()
        assert consistency == Consistency.AT_LEAST_AS_FRESH
        assert zookie == Zookie("local", "500")


# ---------- Zookie wire format ----------


def test_zookie_str_round_trips():
    z = Zookie("local", "100")
    assert str(z) == "local.100"
    assert Zookie.parse("local.100") == z


def test_zookie_parse_rejects_missing_separator():
    with pytest.raises(ValueError):
        Zookie.parse("no-dot-here")


def test_zookie_parse_handles_token_with_dots():
    """``str.split(".", 1)`` — token portion may contain dots
    (SpiceDB tokens are opaque base64-ish strings)."""
    z = Zookie.parse("spicedb.tok.en.with.dots")
    assert z.backend == "spicedb"
    assert z.token == "tok.en.with.dots"
