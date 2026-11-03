"""Django's generated manager exposes every public queryset verb.

Static editor declarations are exercised by tests/typing/consumer_sample.py.
"""

from __future__ import annotations

from rebac.managers import RebacManager, RebacQuerySet

# Queryset methods excluded from this actor-verb contract: Django's own queryset API
# (declared on BaseManager by django-stubs) and per-query mechanics.
QUERYSET_ONLY = {
    "adelete",
    "aggregate",
    "aiterator",
    "as_manager",
    "count",
    "create",
    "exists",
    "explain",
    "iterator",
    "resolve_expression",
}

WRITE_VERBS = {"bulk_create", "bulk_update", "delete", "update"}


def test_manager_exposes_every_queryset_verb() -> None:
    queryset_verbs = {
        name
        for name in dir(RebacQuerySet)
        if not name.startswith("_") and callable(getattr(RebacQuerySet, name))
    }
    declared = {
        name
        for name in dir(RebacManager)
        if not name.startswith("_") and callable(getattr(RebacManager, name))
    }
    missing = sorted(queryset_verbs - declared - QUERYSET_ONLY)
    assert missing == [], f"expose through RebacManager or list as queryset-only: {missing}"
    assert WRITE_VERBS <= queryset_verbs
    assert WRITE_VERBS <= declared, (
        f"inherited writes missing from manager: {WRITE_VERBS - declared}"
    )
    stale = sorted(QUERYSET_ONLY - queryset_verbs)
    assert stale == [], f"no longer a RebacQuerySet method, including inherited: {stale}"
