"""Where a read's candidates are decided, and what it costs; benchmark only (proposal 0025).

    python -m tests.bench.candidates plans --size medium --actor heavy --label storage.File --limit 50 --out DIR
    python -m tests.bench.candidates statements --size medium --actor heavy --scenario transcript --mechanism bounded
    python -m tests.bench.candidates sparse-setup            # a scratch database only (sparse_*)
    python -m tests.bench.candidates sparse --actor 900001,900002 --label provenance.Changeset --limit 50

``plans`` explains, with ANALYZE and BUFFERS, the statement of a page as the
library writes it today and the compiled statement bounded to the first
candidates of the same page (``pk__in``, the library's inline scope), and says
where each spends its time: which id sets (common table expressions) are
computed, whole or not, and how many rows each yields. It shows why the bounded
read decides its candidates by the walk and not by that statement: on a wide
policy the statement builds every id set whatever the candidates.
``statements`` explains each statement one scenario sends under one mechanism.
``sparse-setup`` adds readers of a known small share of one model to a scratch
copy of the benchmark (never to the shared benchmark databases). ``sparse``
reads one page for each of them under each mechanism, against today's page.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

# Sparse readers added to a scratch database: user id -> (share, layout).
# The cast's reserved ids end at 7 and the background starts at 10; these are new users.
SPARSE = {
    900001: ("1/100", "even"),
    900002: ("1/10000", "even"),
    900003: ("1/100", "end"),
    900004: ("1/10000", "end"),
}


def _setup() -> Any:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings_bench")
    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command

    from rebac.testing import install_schema
    from tests.bench import data
    from tests.benchapp import models as standins

    for app in ("contenttypes", "auth", "rebac"):
        call_command("migrate", app, verbosity=0)
    call_command("migrate", run_syncdb=True, verbosity=0)
    install_schema(standins.POLICY)
    return data, settings


def _capture(connection: Any, read: Any) -> list[tuple[str, Any]]:
    statements: list[tuple[str, Any]] = []

    def capture(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        statements.append((sql, params))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(capture):
        read()
    return statements


def _walk(node: dict[str, Any], depth: int = 0) -> Any:
    yield node, depth
    for child in node.get("Plans", ()):
        yield from _walk(child, depth + 1)


def summarise(plan: dict[str, Any]) -> dict[str, Any]:
    """Where a statement's execution goes: its id sets and its outer part."""
    root = plan["Plan"]
    ctes = []
    scans: dict[str, int] = {}
    for node, _depth in _walk(root):
        name = node.get("Subplan Name", "")
        if name.startswith("CTE "):
            ctes.append(
                {
                    "name": name[4:],
                    "rows": node.get("Actual Rows", 0) * node.get("Actual Loops", 1),
                    "loops": node.get("Actual Loops", 1),
                    "ms": round(
                        node.get("Actual Total Time", 0.0) * node.get("Actual Loops", 1), 2
                    ),
                    "never": node.get("Actual Loops", 1) == 0,
                }
            )
        if node.get("Node Type") == "CTE Scan":
            scans[node["CTE Name"]] = scans.get(node["CTE Name"], 0) + 1
    shared = root.get("Shared Hit Blocks", 0) + root.get("Shared Read Blocks", 0)
    return {
        "planning_ms": round(plan.get("Planning Time", 0.0), 1),
        "execution_ms": round(plan.get("Execution Time", 0.0), 1),
        "planning_mib": round(
            (plan.get("Planning", {}) or {}).get("Memory Allocated", 0) / 1024, 1
        ),
        "rows": root.get("Actual Rows"),
        "buffers": shared,
        "ctes": len(ctes),
        "ctes_run": sum(1 for c in ctes if not c["never"]),
        "cte_rows": sum(c["rows"] for c in ctes),
        "cte_ms_top": sorted(ctes, key=lambda c: -c["ms"])[:8],
        "cte_scans": sum(scans.values()),
    }


def trimmed(text: str, keep: int = 60) -> str:
    """The plan's first ``keep`` lines that carry time, with CTE headers."""
    lines = [
        line
        for line in text.splitlines()
        if "actual" in line or line.lstrip().startswith(("CTE ", "InitPlan", "SubPlan"))
    ]
    return "\n".join(lines[:keep])


def plans(arguments: argparse.Namespace) -> None:
    data, settings = _setup()
    from django.db import connections

    from rebac import evaluator_scope

    bench = data.load(settings.REBAC_BENCH_POLICY, arguments.size)
    connection = connections[bench.using]
    actor = (
        bench.actor(arguments.actor)
        if not arguments.actor.isdigit()
        else _subject(bench, arguments.actor)
    )
    model = bench.models[arguments.label]
    out = Path(arguments.out)
    out.mkdir(parents=True, exist_ok=True)
    descending = not arguments.ascending
    order = "-pk" if descending else "pk"
    candidates = list(
        model._base_manager.order_by(order).values_list("pk", flat=True)[: arguments.limit]
    )
    with evaluator_scope():
        admitted = list(
            model.objects.with_actor(actor)
            .order_by(order)
            .values_list("pk", flat=True)[: arguments.limit]
        )
    reads = {
        "today_page": lambda: list(
            model.objects.with_actor(actor).order_by(order)[: arguments.limit]
        ),
        "bounded_candidates": lambda: list(
            model.objects.with_actor(actor).filter(pk__in=candidates)
        ),
        "bounded_admitted": lambda: list(
            model.objects.with_actor(actor).filter(pk__in=admitted or [0])
        ),
    }
    report: dict[str, Any] = {
        "actor": arguments.actor,
        "label": arguments.label,
        "limit": arguments.limit,
        "candidates_admitted": len(set(candidates) & set(admitted)),
    }
    for name, read in reads.items():
        with evaluator_scope():
            read()  # the witness and the profile, kept for the scope
            statements = _capture(connection, read)
        sql, params = max(statements, key=lambda item: len(item[0]))
        memory = ", MEMORY" if connection.pg_version >= 170000 else ""
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON{memory}) " + sql, params)
            plan = cursor.fetchone()[0][0]
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS{memory}) " + sql, params)
            text = "\n".join(row[0] for row in cursor.fetchall())
        stem = f"{arguments.size}_{arguments.actor}_{arguments.label.replace('.', '_')}_{name}"
        (out / f"{stem}.txt").write_text(text)
        (out / f"{stem}.sql").write_text(sql)
        summary = summarise(plan)
        summary["sql_kb"] = round(len(sql) / 1024, 1)
        summary["statements"] = len(statements)
        report[name] = summary
        print(f"== {name}: {json.dumps({k: v for k, v in summary.items() if k != 'cte_ms_top'})}")
        for item in summary["cte_ms_top"]:
            print(
                f"     {item['name']:<40} rows={item['rows']:>8} loops={item['loops']:>3} ms={item['ms']}"
            )
    (
        out / f"{arguments.size}_{arguments.actor}_{arguments.label.replace('.', '_')}.json"
    ).write_text(json.dumps(report, indent=1))


def _subject(bench: Any, user_id: str) -> Any:
    from rebac import SubjectRef

    return SubjectRef.of(bench.user_type, str(user_id))


def sparse_setup(arguments: argparse.Namespace) -> None:
    """Add the SPARSE readers to a scratch database: grants on a share of one model's rows."""
    data, settings = _setup()
    from django.db import connections

    from rebac import ObjectRef, RelationshipTuple
    from rebac.backends import backend

    bench = data.load(settings.REBAC_BENCH_POLICY, arguments.size)
    connection = connections[bench.using]
    if not connection.settings_dict["NAME"].startswith("sparse_"):
        sys.exit("sparse-setup writes: it runs only on a scratch database named sparse_*")
    users = bench.models[bench.user]
    model = bench.models[arguments.label]
    resource_type = bench.entries[arguments.label]["resource_type"]
    keys = list(model._base_manager.order_by("pk").values_list("pk", flat=True))
    template = users._base_manager.order_by("pk").first()
    tuples = []
    for user_id, (share, layout) in SPARSE.items():
        if not users._base_manager.filter(pk=user_id).exists():
            # A copy of an existing user row under the new key.
            template.pk = user_id
            for field in users._meta.concrete_fields:
                if (
                    field.unique
                    and not field.primary_key
                    and isinstance(getattr(template, field.attname), str)
                ):
                    setattr(template, field.attname, f"{field.attname}-{user_id}")
            users._base_manager.bulk_create([template])
        every = int(share.split("/")[1])
        count = max(1, len(keys) // every)
        chosen = keys[::every][:count] if layout == "even" else keys[:count]
        subject = _subject(bench, str(user_id))
        tuples += [
            RelationshipTuple(ObjectRef(resource_type, str(pk)), arguments.relation, subject)
            for pk in chosen
        ]
        print(f"user {user_id}: {share} {layout}: {len(chosen)} of {len(keys)} {arguments.label}")
    backend().write_relationships(tuples)
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE")


def sparse(arguments: argparse.Namespace) -> None:
    """One page per sparse reader under each mechanism, against today's page."""
    data, settings = _setup()
    from django.db import connections
    from django.test import override_settings

    from rebac import evaluator_scope
    from tests.bench.run import MECHANISMS

    bench = data.load(settings.REBAC_BENCH_POLICY, arguments.size)
    connection = connections[bench.using]
    model = bench.models[arguments.label]
    order = "-pk" if not arguments.ascending else "pk"
    rows: list[dict[str, Any]] = []
    for who in arguments.actor.split(","):
        actor = _subject(bench, who) if who.isdigit() else bench.actor(who)
        label = SPARSE.get(int(who), ("", "")) if who.isdigit() else (who, "")
        baseline: Any = None

        def read(actor: Any = actor) -> Any:
            return [
                r.pk for r in model.objects.with_actor(actor).order_by(order)[: arguments.limit]
            ]

        for mechanism in arguments.mechanism.split(","):
            with override_settings(**MECHANISMS[mechanism]):
                times = []
                result: Any = None
                statements: list[Any] = []
                for attempt in range(arguments.repeats + 1):
                    with evaluator_scope():
                        started = perf_counter()
                        if attempt == 0:
                            kept: list[Any] = []
                            statements = _capture(connection, lambda kept=kept: kept.append(read()))
                            result = kept[0]
                        else:
                            read()
                        elapsed = (perf_counter() - started) * 1000
                    if attempt:
                        times.append(elapsed)
            if baseline is None:
                baseline = result
            entry = {
                "actor": who,
                "share": label[0],
                "layout": label[1],
                "mechanism": mechanism,
                "rows": len(result),
                "statements": len(statements),
                "sql_kb": round(sum(len(s) for s, _ in statements) / 1024, 1),
                "wall_ms": round(median(times), 1),
                "first": result[:3],
                "same": result == baseline,
            }
            rows.append(entry)
            print(json.dumps(entry), flush=True)
    if arguments.json:
        Path(arguments.json).write_text(json.dumps(rows, indent=1))


def statements(arguments: argparse.Namespace) -> None:
    """Each statement one scenario sends under one mechanism, explained: size, planning, execution."""
    data, settings = _setup()
    from django.db import connections
    from django.test import override_settings

    from rebac import evaluator_scope
    from tests.bench.run import MECHANISMS

    bench = data.load(settings.REBAC_BENCH_POLICY, arguments.size)
    connection = connections[bench.using]
    actor = bench.actor(arguments.actor)
    scenarios = bench.profile.SCENARIOS
    with override_settings(**MECHANISMS[arguments.mechanism]):

        def run() -> Any:
            with evaluator_scope():
                return scenarios[arguments.scenario](bench, actor)

        run()
        seen = _capture(connection, run)
        memory = ", MEMORY" if connection.pg_version >= 170000 else ""
        totals = [0.0, 0.0, 0.0]
        for index, (sql, params) in enumerate(seen):
            if not sql.lstrip().upper().startswith(("SELECT", "WITH", "(")):
                continue
            with connection.cursor() as cursor:
                cursor.execute(f"EXPLAIN (ANALYZE, FORMAT JSON{memory}) " + sql, params)
                plan = cursor.fetchone()[0][0]
            planning, execution = plan.get("Planning Time", 0.0), plan.get("Execution Time", 0.0)
            totals[0] += len(sql) / 1024
            totals[1] += planning
            totals[2] += execution
            head = " ".join(sql.split())[:110]
            print(
                f"{index:>3} {len(sql) / 1024:>7.1f} KB plan {planning:>7.1f} exec {execution:>7.1f}"
                f" rows {plan['Plan'].get('Actual Rows', 0):>6}  {head}"
            )
        print(
            f"total {len(seen)} statements, {totals[0]:.1f} KB, plan {totals[1]:.1f} ms, exec {totals[2]:.1f} ms"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    explain = commands.add_parser("plans")
    explain.add_argument("--size", default="medium")
    explain.add_argument("--actor", default="heavy")
    explain.add_argument("--label", default="storage.File")
    explain.add_argument("--limit", type=int, default=50)
    explain.add_argument("--ascending", action="store_true")
    explain.add_argument("--out", required=True)
    setup = commands.add_parser("sparse-setup")
    setup.add_argument("--size", default="medium")
    setup.add_argument("--label", default="provenance.Changeset")
    setup.add_argument("--relation", default="actor")
    page = commands.add_parser("sparse")
    page.add_argument("--size", default="medium")
    page.add_argument("--actor", default=",".join(str(k) for k in SPARSE))
    page.add_argument("--label", default="provenance.Changeset")
    page.add_argument("--limit", type=int, default=50)
    page.add_argument("--ascending", action="store_true")
    page.add_argument("--mechanism", default="today,bounded,all,all_bounded")
    page.add_argument("--repeats", type=int, default=3)
    page.add_argument("--json", default="")
    each = commands.add_parser("statements")
    each.add_argument("--size", default="medium")
    each.add_argument("--actor", default="heavy")
    each.add_argument("--scenario", default="files")
    each.add_argument("--mechanism", default="bounded")
    arguments = parser.parse_args()
    {
        "plans": plans,
        "sparse-setup": sparse_setup,
        "sparse": sparse,
        "statements": statements,
    }[arguments.command](arguments)


if __name__ == "__main__":
    main()
