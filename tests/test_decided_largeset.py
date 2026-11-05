"""Actor-side stored sets are decided before and witnessed in statements."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    evaluator_scope,
    sudo,
)
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
ACTOR = SubjectRef.of("auth/user", "alice")


def test_large_decided_set_uses_inline_closure_by_default(monkeypatch):
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation member: auth/user | blog/folder#member
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            permission read = folder->member
        }
    """)
    with sudo(reason="set size fixture"):
        folders = Folder.objects.bulk_create([Folder(name=f"f{i}") for i in range(205)])
        posts = Post.objects.bulk_create(
            [Post(title=f"p{i}", folder=folder) for i, folder in enumerate(folders)]
        )
    key = ("blog/post", "read")

    def measure(count, *, execute=True):
        local.write_relationships(
            [
                RelationshipTuple(ObjectRef("blog/folder", str(folder.pk)), "member", ACTOR)
                for folder in folders[:count]
            ]
        )
        read.reset()
        operation = read._Operation.begin(local, ACTOR, "default")
        sets = operation.sets(operation.verdicts(key))
        if not execute:
            return sets, [], 0, ""
        with evaluator_scope(), CaptureQueriesContext(connection) as queries:
            found = list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True))
        sql = queries[-1]["sql"]
        return sets, found, len(sql.encode()), sql

    one, found_one, one_bytes, _ = measure(1)
    below, found_below, below_bytes, below_sql = measure(4)
    above, found_above, above_bytes, _ = measure(5)
    large, found_large, large_bytes, _ = measure(200)
    assert one is not None and below is not None
    assert above is None and large is None
    assert found_one == [posts[0].pk]
    assert found_below == [post.pk for post in posts[:4]]
    assert found_above == [post.pk for post in posts[:5]]
    assert 350 < (below_bytes - one_bytes) / 3 < 500
    assert above_bytes < 10_000
    assert found_large == [post.pk for post in posts[:200]]
    assert (large_bytes - above_bytes) / 195 < 16
    assert large_bytes < 10_000
    assert "CASE WHEN" not in below_sql
    with monkeypatch.context() as patcher:
        patcher.setattr(read, "_SET_WITNESS_BUDGET", 10**9)
        read.reset()
        lifted, lifted_rows, lifted_bytes, lifted_sql = measure(200)
        assert lifted is not None
        assert lifted_rows == [post.pk for post in posts[:200]]
        assert lifted_sql.count("CASE WHEN") == 0
        assert lifted_bytes < 95_000
