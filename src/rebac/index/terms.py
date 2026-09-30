"""Canonical term identities and race-safe, alias-local interning."""

from collections.abc import Iterable
from itertools import batched
from typing import Any

from django.db import connections, models, transaction
from django.db.models import Q

from rebac.actors import anonymous_actor
from rebac.errors import SchemaError
from rebac.models.index import IndexTerm

from .write import bulk_intern, projected_rows

Triple = tuple[str, str, str]
AUTHENTICATED: Triple = ("$authenticated", "*", "")


def wildcard(type_: str) -> Triple:
    return type_, "*", ""


def anonymous() -> Triple:
    # A stored wildcard of this type has different matching semantics.
    return anonymous_actor().subject_type, "*", "$anonymous"


def type_level(type_: str) -> Triple:
    return type_, "*", "$type"


def _ordered(triples: Iterable[Triple]) -> list[Triple]:
    ordered = list(triples)
    for triple in ordered:
        if len(triple) != 3 or any(not isinstance(v, str) or len(v) > 64 for v in triple):
            raise SchemaError("Index terms require three strings of at most 64 characters.")
        if not triple[0]:
            raise SchemaError("Index term type cannot be empty.")
    return sorted(set(ordered))


def term_ids(triples: Iterable[Triple], *, using: str) -> dict[Triple, int]:
    ordered = _ordered(triples)
    result: dict[Triple, int] = {}
    # Three parameters per identity, bounded below SQLite's expression-depth
    # limit as well as each vendor's parameter limit.
    batch_size = min(200, (connections[using].features.max_query_params or 600) // 3)
    for start in range(0, len(ordered), batch_size):
        lookup = Q()
        for type_, object_id, relation in ordered[start : start + batch_size]:
            lookup |= Q(type=type_, object_id=object_id, relation=relation)
        rows = (
            IndexTerm.objects.using(using)
            .filter(lookup)
            .values_list("type", "object_id", "relation", "pk")
        )
        for type_, object_id, relation, pk in rows:
            result[type_, object_id, relation] = pk
    return result


def intern(triples: Iterable[Triple], *, using: str) -> dict[Triple, int]:
    return bulk_intern(IndexTerm, ("type", "object_id", "relation"), _ordered(triples), using=using)


def intern_from(source: models.QuerySet[Any], *, using: str, distinct: bool = True) -> int:
    """Intern distinct source identities; return the number processed, including existing rows."""
    count = 0
    with transaction.atomic(using=using, savepoint=False):
        rows = projected_rows(
            source.distinct() if distinct else source,
            ("type", "object_id", "relation"),
            using=using,
        )
        for batch in batched(rows, 200, strict=False):
            # Empty actors are internal algebra atoms, never source identities.
            if any(not row[1] for row in batch):
                raise SchemaError("Projected index terms require nonempty type/ID.")
            intern(batch, using=using)
            count += len(batch)
    return count
