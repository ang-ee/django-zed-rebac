"""RebacManager declares every actor verb RebacQuerySet defines, so pyright sees them.

``from_queryset`` copies the methods at runtime; a checker without the Django
plugin only sees the declarations. The two lists are kept in step here.
"""

from __future__ import annotations

from rebac.managers import RebacManager, RebacQuerySet

# Queryset methods a manager does not re-declare: Django's own queryset API
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


def test_manager_declares_every_queryset_verb() -> None:
    queryset_verbs = {
        name
        for name, value in vars(RebacQuerySet).items()
        if not name.startswith("_") and callable(value)
    }
    declared = {name for name in vars(RebacManager) if not name.startswith("_")}
    missing = sorted(queryset_verbs - declared - QUERYSET_ONLY)
    assert missing == [], f"declare on RebacManager or list as queryset-only: {missing}"
    stale = sorted(QUERYSET_ONLY - queryset_verbs)
    assert stale == [], f"no longer a RebacQuerySet method: {stale}"
