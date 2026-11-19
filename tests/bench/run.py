"""Build a bench database and read it: ``python -m tests.bench.run build|read|census|smoke``.

    export DJANGO_SETTINGS_MODULE=tests.settings_bench
    export REBAC_TEST_POSTGRES_URL=postgresql://…/bench        # an empty database
    python -m tests.bench.run build --size medium
    python -m tests.bench.run read --actor heavy,light --scenario inbox,selected --mechanism today,all

``read`` prints, for each scenario, actor and mechanism: the rows read, the
statements and their SQL size, the wall time (median of ``--repeats``), and on
PostgreSQL the planning and execution time and the planner's memory, summed
over the statements. ``census`` says, per model, whether its read permission can
be written as row conditions and how many relations it reaches. ``smoke`` builds
the tiny size and runs every scenario with and without the mechanisms, failing
if a result differs: it needs no database server (in-memory SQLite).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from statistics import median
from time import perf_counter
from typing import Any

MECHANISMS: dict[str, dict[str, Any]] = {
    "today": {},
    "structural": {"REBAC_STRUCTURAL_PREFETCH": True},
    "rows": {"REBAC_ROW_SCOPE": True},
    "named": {"REBAC_ROW_SCOPE": "named"},
    "walk": {"REBAC_BOUNDED_WALK": "decide"},
    "trusted": {"REBAC_TRUSTED_IDS": True},
    "noprofile": {"REBAC_ACTOR_PROFILES": False},
    "all": {
        "REBAC_STRUCTURAL_PREFETCH": True,
        "REBAC_ROW_SCOPE": True,
        "REBAC_BOUNDED_WALK": "decide",
        "REBAC_TRUSTED_IDS": True,
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--size", default="small")
    build.add_argument("--echo", action="store_true")
    read = commands.add_parser("read")
    read.add_argument("--size", default="small", help="the size the database was built at")
    read.add_argument("--scenario", default="")
    read.add_argument("--actor", default="heavy,light,admin")
    read.add_argument("--mechanism", default="today")
    read.add_argument("--repeats", type=int, default=5)
    read.add_argument(
        "--explain", action="store_true", help="planning, execution and planner memory (PostgreSQL)"
    )
    read.add_argument(
        "--uncommitted",
        action="store_true",
        help="read inside a transaction that has written a relationship and not committed it",
    )
    read.add_argument("--json", default="")
    commands.add_parser("smoke")
    census = commands.add_parser("census")
    census.add_argument("--size", default="small")
    census.add_argument("--actor", default="light")
    census.add_argument("--json", default="")
    arguments = parser.parse_args()

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings_bench")
    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command

    from rebac.testing import install_schema
    from tests.bench import data
    from tests.benchapp import models as standins

    # The stand-ins have no migrations and refer to content types: those tables come first.
    for app in ("contenttypes", "auth", "rebac"):
        call_command("migrate", app, verbosity=0)
    call_command("migrate", run_syncdb=True, verbosity=0)
    install_schema(standins.POLICY)
    if arguments.command == "smoke":
        bench = data.load(settings.REBAC_BENCH_POLICY, "tiny")
        data.build(bench)
        everything = argparse.Namespace(
            scenario="",
            actor="heavy,light,outsider,admin",
            mechanism="today,all",
            repeats=1,
            explain=False,
            uncommitted=False,
        )
        failed = [row for row in reads(bench, everything) if "error" in row or not row["same"]]
        sys.exit(f"{len(failed)} read(s) failed or differed" if failed else 0)
    bench = data.load(settings.REBAC_BENCH_POLICY, arguments.size)
    if arguments.command == "build":
        started = perf_counter()
        written = data.build(bench, echo=arguments.echo)
        print(
            f"built {settings.REBAC_BENCH_POLICY} at unit {bench.unit}: "
            f"{sum(bench.rows.values()):,} model rows planned, "
            f"{written.get('relationships', 0):,} relationships, in {perf_counter() - started:.1f} s"
        )
    elif arguments.command == "read":
        report = reads(bench, arguments)
        if arguments.json:
            with open(arguments.json, "w") as handle:
                json.dump(report, handle, indent=1)
    else:
        report = census_of(bench, arguments.actor)
        if arguments.json:
            with open(arguments.json, "w") as handle:
                json.dump(report, handle, indent=1)


def reads(bench: Any, arguments: argparse.Namespace) -> list[dict[str, Any]]:
    from django.db import DatabaseError, connections
    from django.test import override_settings

    from rebac import evaluator_scope, rowscope
    from rebac.compile import read as compiled

    scenarios: dict[str, Any] = bench.profile.SCENARIOS
    names = [name for name in arguments.scenario.split(",") if name] or list(scenarios)
    connection = connections[bench.using]
    report: list[dict[str, Any]] = []
    header = f"{'scenario':<14}{'actor':<9}{'mechanism':<11}{'rows':>5}{'stmts':>6}{'sql KB':>9}{'wall ms':>9}"
    if arguments.explain:
        header += f"{'plan ms':>9}{'exec ms':>9}{'plan MiB':>9}"
    print(header)
    for name in names:
        for who in arguments.actor.split(","):
            actor = bench.actor(who)
            baseline: Any = None
            for mechanism in arguments.mechanism.split(","):
                with override_settings(**MECHANISMS[mechanism]):
                    compiled.reset()
                    rowscope._built.clear()

                    def run(name: str = name, actor: Any = actor) -> Any:
                        with evaluator_scope():
                            return scenarios[name](bench, actor)

                    try:
                        with _transaction(bench, actor, arguments.uncommitted):
                            result = run()
                    except DatabaseError as error:
                        # A statement past REBAC_BENCH_TIMEOUT, most likely.
                        connection.close()
                        reason = str(error).strip().splitlines()[0][:60]
                        report.append(
                            {
                                "scenario": name,
                                "actor": who,
                                "mechanism": mechanism,
                                "error": reason,
                            }
                        )
                        print(f"{name:<14}{who:<9}{mechanism:<11}  {reason}", flush=True)
                        compiled.reset()
                        rowscope._built.clear()
                        continue
                    statements: list[tuple[str, Any]] = []

                    def capture(execute: Any, sql: str, params: Any, many: bool, context: Any,
                                seen: list[tuple[str, Any]] = statements) -> Any:  # fmt: skip
                        seen.append((sql, params))
                        return execute(sql, params, many, context)

                    with _transaction(bench, actor, arguments.uncommitted):
                        with connection.execute_wrapper(capture):
                            run()
                    times = []
                    for _ in range(arguments.repeats):
                        with _transaction(bench, actor, arguments.uncommitted):
                            started = perf_counter()
                            run()
                            times.append((perf_counter() - started) * 1000)
                    row: dict[str, Any] = {
                        "scenario": name,
                        "actor": who,
                        "mechanism": mechanism,
                        "rows": _count(result),
                        "statements": len(statements),
                        "sql_kb": round(sum(len(sql) for sql, _ in statements) / 1024, 1),
                        "wall_ms": round(median(times), 1),
                    }
                    if arguments.explain and connection.vendor == "postgresql":
                        row.update(_explain(connection, statements))
                compiled.reset()
                rowscope._built.clear()
                if baseline is None:
                    baseline = result
                row["same"] = result == baseline
                report.append(row)
                line = (
                    f"{name:<14}{who:<9}{mechanism:<11}{row['rows']:>5}{row['statements']:>6}"
                    f"{row['sql_kb']:>9}{row['wall_ms']:>9}"
                )
                if "plan_ms" in row:
                    line += f"{row['plan_ms']:>9}{row['exec_ms']:>9}{row['plan_mib']:>9}"
                print(line + ("" if row["same"] else "  DIFFERS"), flush=True)
    return report


@contextmanager
def _transaction(bench: Any, actor: Any, uncommitted: bool) -> Iterator[None]:
    """With ``uncommitted``: a transaction that has written one relationship, rolled back after.

    This is how a test that seeds grants inside its own transaction reads, and how a
    request reads after it has written: the library keeps no decision and no profile
    while a relationship write may still roll back.
    """
    if not uncommitted:
        yield
        return
    from django.db import transaction

    from rebac import ObjectRef, RelationshipTuple
    from rebac.backends import backend

    resource_type, relation = bench.profile.UNCOMMITTED_GRANT
    with transaction.atomic(using=bench.using):
        backend().write_relationships(
            [RelationshipTuple(ObjectRef(resource_type, "0"), relation, actor)]
        )
        try:
            yield
        finally:
            transaction.set_rollback(True, using=bench.using)


def _count(result: Any) -> int:
    return len(result) if isinstance(result, (list, tuple, set, dict)) else int(bool(result))


def _explain(connection: Any, statements: list[tuple[str, Any]]) -> dict[str, Any]:
    memory = ", MEMORY" if connection.pg_version >= 170000 else ""
    planning = execution = used = 0.0
    with connection.cursor() as cursor:
        for sql, params in statements:
            if not sql.lstrip().upper().startswith(("SELECT", "WITH", "(")):
                continue
            cursor.execute(f"EXPLAIN (ANALYZE, FORMAT JSON{memory}) " + sql, params)
            plan = cursor.fetchone()[0][0]
            planning += plan.get("Planning Time", 0.0)
            execution += plan.get("Execution Time", 0.0)
            used += (plan.get("Planning", {}) or {}).get("Memory Allocated", 0) / 1024
    return {
        "plan_ms": round(planning, 1),
        "exec_ms": round(execution, 1),
        "plan_mib": round(used, 1),
    }


def census_of(bench: Any, who: str) -> dict[str, Any]:
    from rebac import rowscope, structural

    actor = bench.actor(who)
    policy = structural._policy()
    program, schema = policy.program, policy.schema
    found: dict[str, Any] = {}
    for label, model in sorted(bench.models.items()):
        resource_type = bench.entries[label]["resource_type"]
        key = (resource_type, "read")
        if not resource_type or key not in program.dependencies:
            continue
        reach = program.reachable(key) | {key}
        relations = sum(
            1
            for node in reach
            if (definition := schema.get_definition(node[0])) is not None
            and any(relation.name == node[1] for relation in definition.relations)
        )
        told = rowscope.explain(model, actor, "read")
        found[label] = {"relations": relations, "nodes": len(reach), **told}
    applies = [entry for entry in found.values() if entry["applies"]]
    print(f"{len(found)} models with a read permission; row conditions apply to {len(applies)}")
    for label, entry in sorted(found.items(), key=lambda item: -item[1]["relations"])[:25]:
        print(
            f"  {label:<34}{entry['relations']:>5} relations  {'rows' if entry['applies'] else '; '.join(entry['blocked'])[:90]}"
        )
    return found


if __name__ == "__main__":
    sys.exit(main())
