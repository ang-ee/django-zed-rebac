"""Bounded ORM writes and shared identity interning."""

from collections.abc import Iterable, Iterator, Sequence
from itertools import batched, chain
from pickle import dump, load
from tempfile import TemporaryFile
from typing import Any

from django.db import connections, models, transaction
from django.db.models import Q

from rebac.errors import SchemaError


def projected_rows(
    source: models.QuerySet[Any], fields: Sequence[str], *, using: str, batch_size: int = 256
) -> Iterator[tuple[Any, ...]]:
    """Stream a projection, finishing SQLite's read before any same-table writes.

    SQLite does not isolate an open cursor from writes on that connection.
    Spool its snapshot to a private temporary file to keep memory bounded;
    PostgreSQL cursors and MySQL's buffered driver already isolate this read.
    Only tuples produced here are unpickled, never caller-provided files.
    """
    rows = source.using(using).order_by().values_list(*fields).iterator(chunk_size=batch_size * 4)
    if connections[using].vendor != "sqlite":
        yield from rows
        return
    with TemporaryFile() as snapshot:
        count = 0
        for row in rows:
            dump(row, snapshot)
            count += 1
        snapshot.seek(0)
        for _ in range(count):
            yield load(snapshot)


def stream_create(
    source: models.QuerySet[Any] | Iterable[tuple[Any, ...]],
    model: type[models.Model],
    fields: Sequence[str],
    *,
    using: str,
    batch_size: int = 256,
    **conflict: Any,
) -> int:
    """Create projected rows with model defaults; return rows submitted, not inserted.

    An iterable accepts batches already selected by the expiry-growth writer.
    Conflict handling belongs to the caller and is delegated to bulk_create.
    """
    rows = (
        projected_rows(source, fields, using=using, batch_size=batch_size)
        if isinstance(source, models.QuerySet)
        else iter(source)
    )
    batches = batched(rows, batch_size, strict=False)
    first = next(batches, ())
    if not first:
        return 0
    count = 0
    with transaction.atomic(using=using, savepoint=False):
        for batch in chain((first,), batches):
            objects = [model(**dict(zip(fields, row, strict=True))) for row in batch]
            model._default_manager.using(using).bulk_create(
                objects, batch_size=batch_size, **conflict
            )
            count += len(objects)
    return count


def bulk_intern[K: tuple[str, ...]](
    model: type[models.Model],
    key_fields: Sequence[str],
    keys: Iterable[K],
    *,
    using: str,
    batch_size: int = 200,
) -> dict[K, int]:
    """Intern exact identities and validate every result, including ignored errors."""
    if not 1 <= batch_size <= 200:
        raise ValueError("Interning batch_size must be between 1 and 200")
    ordered = sorted(set(keys))
    if not ordered:
        return {}
    result: dict[K, int] = {}
    batch_size = min(
        batch_size, (connections[using].features.max_query_params or 600) // len(key_fields)
    )
    with transaction.atomic(using=using, savepoint=False):
        for batch in batched(ordered, batch_size, strict=False):
            manager = model._default_manager.using(using)
            objects = [model(**dict(zip(key_fields, key, strict=True))) for key in batch]
            supports_update = connections[using].features.supports_update_conflicts
            manager.bulk_create(
                objects,
                update_conflicts=supports_update,
                ignore_conflicts=not supports_update,
                update_fields=[key_fields[-1]] if supports_update else None,
                unique_fields=key_fields
                if supports_update
                and connections[using].features.supports_update_conflicts_with_target
                else None,
                batch_size=batch_size,
            )
            if all(object_.pk is not None for object_ in objects):
                result.update(
                    (key, object_.pk) for key, object_ in zip(batch, objects, strict=True)
                )
                continue
            lookup = Q()
            for key in batch:
                lookup |= Q(**dict(zip(key_fields, key, strict=True)))
            found = {
                tuple(row[:-1]): row[-1]
                for row in manager.filter(lookup).values_list(*key_fields, "pk")
            }
            missing = [key for key in batch if key not in found]
            if missing:
                raise SchemaError(
                    f"{model.__name__} interning failed to retrieve {len(missing)} identities: "
                    f"{missing[:3]!r}"
                )
            result.update((key, found[key]) for key in batch)
    return result
