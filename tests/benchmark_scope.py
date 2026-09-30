"""Run with ``uv run --no-sync pytest -q -s tests/benchmark_scope.py``."""

from __future__ import annotations

import json
from statistics import median
from time import perf_counter

import pytest

from rebac import SubjectRef, backend
from rebac.index import read
from rebac.index.program import program_for
from rebac.models.generation import SchemaGeneration
from rebac.schema import parse_zed
from tests.testapp.models import Post

SCHEMA = """
definition auth/user {}
definition auth/group { relation member: auth/user | auth/group#member }
definition blog/post {
    relation viewer: auth/user | auth/group#member | auth/user:*
    relation blocked: auth/user | auth/group#member
    permission p1 = viewer - blocked
    permission p2 = p1 - blocked
    permission p3 = p2 - blocked
    permission p4 = p3 - blocked
    permission p5 = p4 - blocked
    permission read = p5 + authenticated
}
"""


@pytest.mark.django_db
def test_benchmark_scope() -> None:
    active = backend()
    active.set_schema(parse_zed(SCHEMA))
    revision = active._manual_schema_revision()
    program = program_for(active, using="default")
    SchemaGeneration.objects.update_or_create(
        pk=1,
        defaults={
            "revision": revision,
            "index_revision": revision,
            "index_program": program.digest,
        },
    )
    actor = SubjectRef.of("auth/user", "alice")

    def measure(fn, repetitions=11):
        times = []
        result = None
        for _ in range(repetitions):
            started = perf_counter()
            result = fn()
            times.append((perf_counter() - started) * 1000)
        return round(median(times), 3), result

    build_ms, predicate = measure(
        lambda: read.scope_q(Post, action="read", actor=actor, using="default")
    )
    compile_ms, _ = measure(lambda: Post._base_manager.filter(predicate).query.sql_with_params())
    scoped_ms, scoped = measure(
        lambda: Post.objects.with_actor(actor).scoped().query.sql_with_params()
    )
    sql, params = scoped
    new_actor_ms, _ = measure(
        lambda: (
            Post.objects.with_actor(SubjectRef.of("auth/user", "bob"))
            .scoped()
            .query.sql_with_params()
        ),
        repetitions=1,
    )
    read._scope_cache.clear()
    cold_ms, _ = measure(
        lambda: Post.objects.with_actor(actor).scoped().query.sql_with_params(),
        repetitions=1,
    )
    print(
        "SCOPE_BENCH "
        + json.dumps(
            {
                "build_ms": build_ms,
                "compile_ms": compile_ms,
                "scoped_ms": scoped_ms,
                "cold_scoped_ms": cold_ms,
                "new_actor_ms": new_actor_ms,
                "sql_chars": len(sql),
                "params": len(params),
                "exists": sql.upper().count("EXISTS"),
            },
            sort_keys=True,
        )
    )
