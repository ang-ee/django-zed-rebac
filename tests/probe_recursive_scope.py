"""Reproduce recursive SQL and measure the old enumeration integration.

Run: uv run --no-sync python -m tests.probe_recursive_scope > recursive-scope-probe.txt
Uses tests.settings's private in-memory database, never an application database.
"""

from __future__ import annotations

import io
import os
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import django

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.settings"
django.setup()

from django.apps import apps  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection, reset_queries  # noqa: E402
from django.test import override_settings  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from rebac import (  # noqa: E402
    PermissionDepthExceeded,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend  # noqa: E402
from rebac.backends.local_query import LocalQueryScope  # noqa: E402
from rebac.conf import app_settings  # noqa: E402
from tests.test_recursive_queryscope import (  # noqa: E402
    ACTOR,
    ANONYMOUS,
    OUTSIDER,
    chain,
    grant,
    no_enumeration,
    recursive_schema,
)
from tests.testapp.models import Folder  # noqa: E402


def sync(source, shape, backing, *, builtin=False):
    text, member, hop, action = recursive_schema(shape, backing, builtin=builtin)
    source.write_text(text)
    with patch.object(apps.get_app_config("testapp"), "rebac_schema", str(source)):
        call_command("rebac", "sync", "--force-overwrite", "--yes", stdout=io.StringIO())
        call_command("rebac", "sync", "--check", stdout=io.StringIO())
    reset_backend()
    active = backend()
    active.schema()
    return active, member, hop, action


def clear_rows():
    with sudo(reason="recursive probe cleanup"):
        Folder._base_manager.all().delete()


def proof(source, storage, shape, backing):
    active, member, hop, action = sync(source, shape, backing)
    limit = app_settings.REBAC_DEPTH_LIMIT
    for depth in (0, 1, 2, limit):
        clear_rows()
        rows = chain(active, hop, backing, depth)
        actors = [SubjectRef.of("auth/user", str(100 + i)) for i in range(len(rows))]
        for row, actor in zip(rows, actors, strict=True):
            grant(active, row, member, actor)
        with no_enumeration(active):
            predicate = LocalQueryScope(active, actors[0], "default").predicate(
                Folder, action, "blog/folder"
            )
            query = Folder._base_manager.filter(predicate)
            if depth == 0:
                print(f"\nSQL {storage}/{shape}/{backing} (bound={limit}):\n{query.query}")
            for level, actor in enumerate([*actors, OUTSIDER, ANONYMOUS]):
                expected = {row.pk for row in rows[level:]} if level < len(rows) else set()
                actual = set(
                    Folder.objects.with_actor(actor)
                    .with_action(action)
                    .values_list("pk", flat=True)
                )
                assert actual == expected
                for row in rows:
                    assert active.check_access(
                        subject=actor, action=action, resource=to_object_ref(row)
                    ).allowed is (row.pk in expected)
        print(
            f"PASS {storage}/{shape}/{backing}: depth {depth}, every ancestor, outsider, anonymous"
        )
    clear_rows()
    rows = chain(active, hop, backing, limit + 1)
    grant(active, rows[0], member)
    errors = []
    with no_enumeration(active):
        for evaluate in (
            lambda: active.check_access(
                subject=ACTOR, action=action, resource=to_object_ref(rows[-1])
            ),
            lambda: (
                Folder.objects.with_actor(ACTOR).with_action(action).filter(pk=rows[-1].pk).exists()
            ),
        ):
            try:
                evaluate()
            except PermissionDepthExceeded as error:
                errors.append(str(error))
    assert errors == [f"Depth limit {limit} exceeded"] * 2
    print(f"PASS {storage}/{shape}/{backing}: depth {limit + 1} both raise {errors[0]}")
    clear_rows()
    active, member, hop, action = sync(source, shape, backing, builtin=True)
    rows = chain(active, hop, backing, 2)
    Folder._base_manager.filter(pk=rows[0].pk).update(is_active=True)
    with no_enumeration(active):
        assert Folder.objects.with_actor(OUTSIDER).with_action(action).count() == 3
        assert Folder.objects.with_actor(ANONYMOUS).with_action(action).count() == 0
    print(f"PASS {storage}/{shape}/{backing}: recursive authenticated arm compiles")
    clear_rows()


def measurement(source, storage, shape, backing, *, configured_bound=False):
    active, member, hop, action = sync(source, shape, backing)
    result = []
    for depth in (1, 3, app_settings.REBAC_DEPTH_LIMIT):
        for count in (1, 10, 100):
            clear_rows()
            rows = chain(active, hop, backing, depth - 1)
            grant(active, rows[0], member)
            with sudo(reason="recursive scope measurement"):
                leaves = Folder.objects.bulk_create(
                    [
                        Folder(
                            name=f"leaf-{i}",
                            is_active=False,
                            parent=rows[-1] if backing == "field" else None,
                        )
                        for i in range(count)
                    ]
                )
            if backing == "tuple":
                active.write_relationships(
                    [
                        RelationshipTuple(
                            to_object_ref(leaf), hop, SubjectRef.of("blog/folder", str(rows[-1].pk))
                        )
                        for leaf in leaves
                    ]
                )
            metrics = []
            for before in (True, False):
                # This is exactly the previous integration seam: unsupported
                # scope -> accessible IDs -> WHERE id IN (...).
                context = (
                    patch.object(active, "queryset_filter", return_value=None)
                    if before
                    else nullcontext()
                )
                reset_queries()
                with (
                    override_settings(REBAC_DEPTH_LIMIT=depth)
                    if configured_bound
                    else nullcontext(),
                    context,
                    CaptureQueriesContext(connection) as captured,
                ):
                    actual = list(
                        Folder.objects.with_actor(ACTOR)
                        .with_action(action)
                        .filter(name__startswith="leaf-")
                        .values_list("pk", flat=True)
                    )
                assert len(actual) == count
                statements = [q["sql"] for q in captured.captured_queries]
                metrics.extend(
                    (
                        len(statements),
                        len(statements[-1].encode()),
                        sum(len(s.encode()) for s in statements),
                    )
                )
            result.append([storage, shape, backing, depth, count, *metrics])
    clear_rows()
    return result


def main():
    call_command("migrate", verbosity=0)
    table = []
    bound_table = []
    with TemporaryDirectory(prefix="rebac-recursive-") as directory:
        source = Path(directory) / "permissions.zed"
        for storage in ("denormalized", "registry"):
            with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
                for shape in ("role", "folder"):
                    for backing in ("tuple", "field"):
                        proof(source, storage, shape, backing)
                        table.extend(measurement(source, storage, shape, backing))
                        bound_table.extend(
                            measurement(source, storage, shape, backing, configured_bound=True)
                        )
    print("\nMEASUREMENTS — fixed default bound 8; depth is data-chain length")
    print_table(table)
    print("\nBOUND SCALING — REBAC_DEPTH_LIMIT equals data-chain depth")
    print_table(bound_table)


def print_table(table):
    print("SQL bytes as captured by SQLite; schema revision reads included.")
    print(
        "| Store | Shape | Hop | Depth | Leaf rows | Before queries | Before read bytes | Before total bytes | After queries | After read bytes | After total bytes |"
    )
    print("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in table:
        print("| " + " | ".join(map(str, row)) + " |")


if __name__ == "__main__":
    main()
