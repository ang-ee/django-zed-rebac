"""Native-ID boundary sharing must preserve PostgreSQL execution cost."""

from __future__ import annotations

import json
from statistics import median
from typing import Any

import pytest
from django.db import connection, models
from django.db.models import Expression

from rebac import SubjectRef, sudo
from rebac.backends.local import mark_relationships_changed
from rebac.codec import identity_codec
from rebac.compile import read
from rebac.compile.predicate import Compiler
from rebac.managers import share_scoped_definitions
from rebac.models.relationship import Relationship
from rebac.testing import install_schema
from tests.testapp.models import Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta, pytest.mark.slow]


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL planner regression")
@pytest.mark.parametrize("shape", ["exclusion", "union"])
def test_native_projection_stays_near_inline_guard_at_3000_posts(monkeypatch, shape):
    install_schema("""
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user
            relation editor: auth/user
            relation banned: auth/user
            relation parent: blog/post
            permission read = ((viewer + editor) + parent->read) - banned
            permission view = ((viewer + editor) + parent->view) - banned
        }
    """)
    actor = SubjectRef.of("auth/user", "native-latency")
    other = SubjectRef.of("auth/user", "native-latency-other")
    with sudo(reason="native latency fixture"):
        posts = Post.objects.bulk_create([Post(title=f"post-{i}") for i in range(3000)])
    relationships = [
        Relationship(
            resource_type="blog/post",
            resource_id=str(post.pk),
            relation=relation,
            subject_type=subject.subject_type,
            subject_id=subject.subject_id,
        )
        for post in posts
        for relation, subject in (("viewer", actor), ("editor", other))
    ]
    relationships.extend(
        Relationship(
            resource_type="blog/post",
            resource_id=str(posts[i].pk),
            relation="parent",
            subject_type="blog/post",
            subject_id=str(posts[i + 1].pk),
        )
        for i in range(579)
    )
    relationships.extend(
        Relationship(
            resource_type="blog/post",
            resource_id=str(posts[i].pk),
            relation="banned",
            subject_type=actor.subject_type,
            subject_id=actor.subject_id,
        )
        for i in range(300)
    )
    Relationship._base_manager.bulk_create(relationships, batch_size=1000)
    mark_relationships_changed()
    with connection.cursor() as cursor:
        cursor.execute(f"ANALYZE {Post._meta.db_table}")
        cursor.execute(f"ANALYZE {Relationship._meta.db_table}")

    def statement() -> tuple[str, tuple[Any, ...]]:
        read.reset()
        if shape == "exclusion":
            query = Post.objects.with_actor(actor).with_action("read").scoped_for_aggregate()
        else:
            branches = [
                Post.objects.with_actor(actor).with_action(action).scoped().values("pk")
                for action in ("read", "view")
            ]
            plain = [
                models.QuerySet(model=Post, query=branch.query.clone(), using=branch.db)
                for branch in branches
            ]
            query = share_scoped_definitions(plain[0].union(plain[1], all=True))
        return query.query.sql_with_params()

    def measure() -> dict[str, Any]:
        sql, params = statement()
        samples = []
        with connection.cursor() as cursor:
            for _ in range(3):
                cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
                samples.append(cursor.fetchone()[0][0])
        return {
            "bytes": len(sql.encode()),
            "planning_ms": round(median(item["Planning Time"] for item in samples), 3),
            "execution_ms": round(median(item["Execution Time"] for item in samples), 3),
            "native_ctes": sql.count('"rebacnative_'),
        }

    def inline_native(self, model, identity, ref: Expression, resource_type):
        return identity_codec(model, identity).to_column(ref)

    native_reference = Compiler._native_reference

    def force_native(self, model, identity, ref: Expression, resource_type):
        result = native_reference(self, model, identity, ref, resource_type)
        self._native_decisions.update(dict.fromkeys(self._native_ctes, True))
        return result

    with monkeypatch.context() as baseline:
        baseline.setattr(Compiler, "_native_reference", inline_native)
        reference = measure()
    with monkeypatch.context() as forced:
        forced.setattr(Compiler, "_native_reference", force_native)
        forced.setattr(Compiler, "_native_share_threshold", lambda self: 2)
        forced_shared = measure()
    candidate = measure()
    print(
        "NATIVE_LATENCY "
        + json.dumps(
            {
                "shape": shape,
                "reference": reference,
                "forced_shared": forced_shared,
                "candidate": candidate,
            },
            sort_keys=True,
        )
    )
    assert forced_shared["native_ctes"] > 0
    assert candidate["native_ctes"] == 0
    assert candidate["bytes"] == reference["bytes"]
    # Both statements have the same shape. Broad same-run bounds catch a
    # persistent plan regression while allowing host scheduling noise.
    assert candidate["execution_ms"] <= reference["execution_ms"] * 2 + 20
    assert candidate["planning_ms"] <= reference["planning_ms"] * 2 + 20
