"""Decided statements remain request-local across revisions and cache pressure."""

import pytest
from django.test import override_settings

from rebac import evaluator_scope
from rebac.compile import read
from tests.test_compile_exact_root import (  # noqa: F401
    ALICE,
    SCHEMA,
    records,
)
from tests.testapp.models import Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


def test_alternating_and_cycling_revisions(records, monkeypatch):  # noqa: F811
    local, allowed, _denied, _grant = records
    snapshot = local._schema_snapshot(using="default")
    current = {"revision": snapshot.revision}
    monkeypatch.setattr(
        local, "_schema_snapshot", lambda *a, **k: snapshot._replace(revision=current["revision"])
    )
    read.reset()
    trace = []

    def read_as(revision):
        current["revision"] = revision
        with evaluator_scope():
            rows = list(Post.objects.with_actor(ALICE).values_list("pk", flat=True))
        s = read.statement_cache_stats()
        trace.append((revision, rows, s["entries"], s["hits"], s["misses"], s["evictions"]))
        return rows

    real = snapshot.revision
    # Two readers pinned to different revisions alternate 6 times.
    for step in range(6):
        read_as(real if step % 2 == 0 else "pinned-old")
    alternating = read.statement_cache_stats()
    # Cycle through five more distinct revisions: the oldest observed must retire.
    for name in ("r2", "r3", "r4", "r5"):
        read_as(name)
    cycled = read.statement_cache_stats()
    assert alternating["evictions"] == 0
    assert alternating["entries"] == alternating["misses"] == 0
    # the real revision answers; fake revisions are fenced (revision mismatch at run time)
    assert trace[0][1] == [allowed.pk] and trace[1][1] == []
    assert cycled["entries"] == 0  # decided statements never enter the cache


def test_byte_bound_and_eviction_during_use(records):  # noqa: F811
    local, allowed, denied, _grant = records
    read.reset()
    list(Post.objects.with_actor(ALICE).filter(pk=allowed.pk))
    one = read.statement_cache_stats()["bytes"]
    with override_settings(REBAC_STATEMENT_CACHE_BYTES=one + 10):
        read.reset()
        operation = read._Operation.begin(local, ALICE, "default")
        held = read._scope_statement(operation, Post, ("blog/post", "read"))
        # force eviction of the held statement by filling the cache with another shape
        list(Post.objects.with_actor(ALICE).filter(pk=allowed.pk))
        list(Post.objects.with_actor(ALICE).filter(pk__in=[allowed.pk, denied.pk]))
        stats = read.statement_cache_stats()
        assert stats["bytes"] <= one + 10
        assert stats["evictions"] == 0
        # A request-local decided statement stays usable by its holder.
        from django.db import connection

        from rebac.compile.read import _bound_params

        params = _bound_params(
            held.params,
            backend=local,
            actor=ALICE,
            using="default",
            verdicts=operation.verdicts(("blog/post", "read")),
        )
        sql = f"SELECT id FROM testapp_post WHERE id IN {held.sql}"
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            assert [r[0] for r in cursor.fetchall()] == [allowed.pk]
