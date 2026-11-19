"""``rebac.skeleton``: a policy and what it reads of its models, with no row read."""

from __future__ import annotations

import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import skeleton
from rebac.testing import install_schema
from tests.mechanism_policies import FIXED

pytestmark = pytest.mark.django_db


def test_the_skeleton_holds_the_models_the_policy_touches_and_what_it_reads_of_them():
    backend = install_schema(FIXED)
    with CaptureQueriesContext(connection) as captured:
        found = skeleton.skeleton(backend.schema())
    assert not [query for query in captured.captured_queries if "testapp_" in query["sql"]]
    models = found["models"]
    # The three protected models, and the user and group models the subject types name.
    assert set(models) == {
        "auth.User",
        "auth.Group",
        "testapp.ScopeThread",
        "testapp.ScopeMessage",
        "testapp.ScopePart",
    }
    assert models["auth.User"]["is_user"] and not models["testapp.ScopePart"]["is_user"]
    assert found["notes"] == []
    thread = {field["name"]: field for field in models["testapp.ScopeThread"]["fields"]}
    # The key, and the column a filtered constant reads; nothing else of the table.
    assert set(thread) == {"id", "label"}
    assert thread["id"]["pk"] and thread["label"]["kind"] == "text"
    part = {field["name"]: field for field in models["testapp.ScopePart"]["fields"]}
    assert (
        part["message"]["kind"] == "foreign_key" and part["message"]["to"] == "testapp.ScopeMessage"
    )
    assert part["fragment_id"]["kind"] == "int"  # a filtered field backing reads it
    assert models["testapp.ScopePart"]["resource_type"] == "scope/part"
    assert models["testapp.ScopePart"]["db_table"] == "testapp_scopepart"


def test_export_writes_the_policy_and_the_skeleton(tmp_path):
    install_schema(FIXED)
    found = skeleton.export(tmp_path, source={"repository": "example/project", "commit": "abc"})
    written = json.loads((tmp_path / "skeleton.json").read_text())
    assert written == found and written["source"]["commit"] == "abc"
    policy = (tmp_path / "policy.zed").read_text()
    assert "definition scope/part" in policy and "rebac:field" in policy
    # The exported policy is the policy: it installs again and names the same types.
    again = install_schema(policy)
    assert {d.resource_type for d in again.schema().definitions} == {
        "auth/user", "auth/group", "scope/role", "scope/thread", "scope/message", "scope/part",
    }  # fmt: skip
