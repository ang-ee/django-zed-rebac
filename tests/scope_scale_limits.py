"""Frozen 0.33.0 scale limitations; identity drift is always a hard failure.

SHA256 fingerprints cover raw UTF-8 SQL, canonical JSON ordered parameters, and
recursive structural plans (the release comparison's keys, excluding estimates
and measurements). Only the measured vendor, dimensions and work_mem qualify.
The caller records individual subtest xfails after these guards, never a fixture
xfail. A resolved rescan fails explicitly, like pytest's strict XPASS.
"""

import hashlib
import json
from functools import cache
from pathlib import Path

SHAPE_KEYS = (
    "Node Type",
    "Parent Relationship",
    "Parallel Aware",
    "Async Capable",
    "Join Type",
    "Inner Unique",
    "Relation Name",
    "Schema",
    "Alias",
    "Index Name",
    "CTE Name",
    "Subplan Name",
    "Scan Direction",
    "Strategy",
    "Partial Mode",
    "Filter",
    "Index Cond",
    "Recheck Cond",
    "Hash Cond",
    "Merge Cond",
    "Join Filter",
    "One-Time Filter",
    "Sort Key",
    "Group Key",
    "Grouping Sets",
    "Output",
    "Cache Key",
    "Cache Mode",
    "Workers Planned",
)
FOLLOW_UP = "proposal 0018, Known limitations / follow-up"


def plan_shape(node):
    return {
        **{key: node[key] for key in SHAPE_KEYS if key in node},
        "Plans": [plan_shape(child) for child in node.get("Plans", [])],
    }


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def query_fingerprints(sql, params):
    return {
        "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
        "params_sha256": fingerprint(list(params)),
    }


@cache
def manifest():
    return json.loads((Path(__file__).parent / "fixtures/scope_scale_0330.json").read_text())


def known_limitation(report, key):
    frozen = manifest()
    if any(report[name] != frozen[name] for name in ("messages", "parts", "work_mem")):
        return None
    fixture = "-".join(report[name] for name in ("storage", "shape", "scope_mode"))
    return frozen["cells"].get(f"{report['postgres'] // 10000}/{fixture}/{key}")


def assert_query_identity(known, sql, params):
    for name, actual in query_fingerprints(sql, params).items():
        assert actual == known[name], f"0.33.0 known-limitation identity drift: {name}"


def assert_plan_identity(known, plan):
    assert fingerprint(plan_shape(plan)) == known["plan_sha256"], (
        "0.33.0 known-limitation identity drift: structural plan"
    )


def assert_rescan_failure(known, rescans):
    assert rescans != 0, f"XPASS(strict): 0.33.0 CTE rescan resolved; remove entry; {FOLLOW_UP}"
    assert rescans == known["rescans"], "0.33.0 known-limitation identity drift: rescan count"
