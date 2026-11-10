"""Known limitations must not admit a changed query, plan, or failure."""

from collections import Counter

import pytest

from tests.scope_scale_limits import (
    assert_plan_identity,
    assert_query_identity,
    assert_rescan_failure,
    fingerprint,
    known_limitation,
    manifest,
    plan_shape,
    query_fingerprints,
)


def test_manifest_contains_only_the_measured_limitations():
    cells = manifest()["cells"]
    assert Counter((key.split("/")[0], value["kind"]) for key, value in cells.items()) == {
        ("16", "timeout"): 8,
        ("17", "timeout"): 8,
        ("17", "rescan"): 56,
    }
    admins = [key for key, value in cells.items() if "-admin-" in key]
    assert len(admins) == 8
    assert all("-outside/" in key for key in admins)
    assert all(value["rescans"] == 1 for value in cells.values() if value["kind"] == "rescan")


@pytest.mark.parametrize("change", ["sql", "params", "parameter_order", "plan", "child_plan"])
def test_changed_identity_is_a_hard_failure(change):
    sql, params = "SELECT %s, %s", [1, 2]
    plan = {"Node Type": "Result", "Plans": [{"Node Type": "CTE Scan"}]}
    frozen = {**query_fingerprints(sql, params), "plan_sha256": fingerprint(plan_shape(plan))}
    assert_query_identity(frozen, sql, params)
    assert_plan_identity(frozen, plan)
    if change == "sql":
        sql += " "
    elif change == "params":
        params = [1, "2"]
    elif change == "parameter_order":
        params.reverse()
    elif change == "plan":
        plan["Filter"] = "false"
    else:
        plan["Plans"][0]["Node Type"] = "Seq Scan"
    with pytest.raises(AssertionError, match="identity drift"):
        assert_query_identity(frozen, sql, params)
        assert_plan_identity(frozen, plan)


@pytest.mark.parametrize("rescans, message", [(0, r"XPASS\(strict\)"), (2, "rescan count")])
def test_rescan_fix_or_different_failure_is_not_xfailed(rescans, message):
    assert_rescan_failure({"rescans": 1}, 1)
    with pytest.raises(AssertionError, match=message):
        assert_rescan_failure({"rescans": 1}, rescans)


@pytest.mark.parametrize(
    "changed",
    [{"messages": 1000}, {"parts": 10000}, {"work_mem": "8MB"}, {"postgres": 180000}],
)
def test_unmeasured_configuration_cannot_use_known_limitation(changed):
    report = dict(
        storage="denormalized",
        shape="single",
        scope_mode="outside",
        messages=50000,
        parts=500000,
        work_mem="4MB",
        postgres=170000,
    )
    key = "off-member-broad-listing"
    assert known_limitation(report, key) is not None
    assert known_limitation({**report, **changed}, key) is None
    assert known_limitation(report, "off-admin-broad-count") is None
