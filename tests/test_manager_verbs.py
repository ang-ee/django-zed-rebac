"""Django's generated manager exposes every public queryset verb.

Static editor declarations are exercised by tests/typing/consumer_sample.py.
"""

from __future__ import annotations

from rebac.managers import RebacManager, RebacQuerySet

# Queryset methods excluded from this actor-verb contract: Django's own queryset API
# (declared on BaseManager by django-stubs) and per-query mechanics.
QUERYSET_ONLY = {
    "aggregate",
    "aiterator",
    "bulk_create",
    "bulk_update",
    "count",
    "create",
    "delete",
    "exists",
    "explain",
    "iterator",
    "resolve_expression",
    "update",
}


def test_manager_exposes_every_queryset_verb() -> None:
    queryset_verbs = {
        name
        for name, value in vars(RebacQuerySet).items()
        if not name.startswith("_") and callable(value)
    }
    declared = {
        name
        for name in dir(RebacManager)
        if not name.startswith("_") and callable(getattr(RebacManager, name))
    }
    missing = sorted(queryset_verbs - declared - QUERYSET_ONLY)
    assert missing == [], f"expose through RebacManager or list as queryset-only: {missing}"
    stale = sorted(QUERYSET_ONLY - queryset_verbs)
    assert stale == [], f"no longer a RebacQuerySet method: {stale}"
