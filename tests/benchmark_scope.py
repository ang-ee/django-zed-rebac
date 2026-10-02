"""Run with ``uv run --no-sync pytest -q -s tests/benchmark_scope.py``."""

from __future__ import annotations

import json
from statistics import median
from time import perf_counter

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, SubjectRef
from rebac.compile import read
from rebac.testing import install_schema
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
ROWS = 200


@pytest.mark.django_db
def test_benchmark_scope() -> None:
    active = install_schema(SCHEMA)
    Post._base_manager.bulk_create([Post(title=f"post {number}") for number in range(ROWS)])
    actor = SubjectRef.of("auth/user", "alice")
    resource = ObjectRef("blog/post", str(Post._base_manager.values_list("pk", flat=True)[0]))

    def measure(fn, repetitions=11):
        times = []
        result = None
        for _ in range(repetitions):
            started = perf_counter()
            result = fn()
            times.append((perf_counter() - started) * 1000)
        return round(median(times), 3), result

    def statements(fn):
        with CaptureQueriesContext(connection) as queries:
            fn()
        return len(queries)

    def scoped_sql(subject):
        return Post.objects.with_actor(subject).scoped().query.sql_with_params()

    def rows():
        return list(Post.objects.with_actor(actor).scoped())

    def check():
        return active.check_access(subject=actor, action="read", resource=resource)

    build_ms, predicate = measure(
        lambda: read.scope_q(
            backend=active, model=Post, action="read", actor=actor, using="default"
        )
    )
    compile_ms, _ = measure(lambda: Post._base_manager.filter(predicate).query.sql_with_params())
    scoped_ms, scoped = measure(lambda: scoped_sql(actor))
    sql, params = scoped
    new_actor_ms, _ = measure(lambda: scoped_sql(SubjectRef.of("auth/user", "bob")), repetitions=1)
    read.reset()
    cold_ms, _ = measure(lambda: scoped_sql(actor), repetitions=1)
    rows_ms, found = measure(rows)
    check_ms, decision = measure(check)
    assert len(found) == ROWS and decision.allowed
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
                "rows_ms": rows_ms,
                "rows_statements": statements(rows),
                "check_ms": check_ms,
                "check_statements": statements(check),
            },
            sort_keys=True,
        )
    )
