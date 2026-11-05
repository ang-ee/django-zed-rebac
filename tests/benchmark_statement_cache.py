"""Run with ``uv run --no-sync pytest -q -s tests/benchmark_statement_cache.py``."""

from __future__ import annotations

import json
from time import perf_counter
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import CheckItem, ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.compile import read
from rebac.compile.predicate import Compiler
from rebac.evaluator import evaluator_scope
from rebac.expressions import permission_expression
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

SCHEMA = """
caveat active(flag bool) { flag }
definition auth/user {}
definition auth/group { relation member: auth/user }
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation caveated: auth/user with active
    relation admin: auth/group#member
    permission read = folder->read + admin
    permission view = folder->read
    permission edit = admin
    permission caveated_read = folder->read + caveated
}
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.slow
def test_statement_cache_benchmark(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = install_schema(SCHEMA)
    actor = SubjectRef.of("auth/user", "alice")
    admin = SubjectRef.of("auth/user", "admin")
    with sudo(reason="statement-cache benchmark fixture"):
        folder = Folder.objects.create(name="allowed")
        posts = Post._base_manager.bulk_create(
            [Post(title=f"post-{number}", folder=folder) for number in range(50)]
        )
    backend.write_relationships(
        [
            RelationshipTuple(to_object_ref(folder), "viewer", actor),
            RelationshipTuple(ObjectRef("auth/group", "admins"), "member", admin),
            *[
                RelationshipTuple(
                    to_object_ref(post),
                    "admin",
                    SubjectRef.of("auth/group", "admins", "member"),
                )
                for post in posts
            ],
            RelationshipTuple(
                to_object_ref(posts[0]),
                "caveated",
                actor,
                caveat_name="active",
                caveat_context={"flag": True},
            ),
        ]
    )
    ids = [post.pk for post in posts]
    bulk = [
        CheckItem(actor, "read", ObjectRef("blog/post", str(number))) for number in range(1, 301)
    ]

    def point() -> object:
        return list(Post.objects.with_actor(actor).filter(pk=ids[0]).values_list("pk", flat=True))

    def many() -> object:
        return list(Post.objects.with_actor(actor).filter(pk__in=ids).values_list("pk", flat=True))

    def scope() -> object:
        return list(Post.objects.with_actor(actor).values_list("pk", flat=True))

    def caveated_scope() -> object:
        return list(
            Post.objects.with_actor(actor).with_action("caveated_read").values_list("pk", flat=True)
        )

    def multi_action_scope() -> object:
        actions = ("read", "view", "edit")
        handle = Post.objects.with_actor(actor).filter(pk__in=ids).scope_cte(actions=actions)
        projected = Post._base_manager.filter(pk__in=ids).annotate(
            **{
                f"can_{action}": permission_expression(Post, action, actor=actor)
                for action in actions
            }
        )
        return list(handle.attach(projected).values_list("pk", "can_read", "can_view", "can_edit"))

    def multi_action_caveated() -> object:
        actions = ("read", "caveated_read")
        handle = Post.objects.with_actor(actor).filter(pk__in=ids).scope_cte(actions=actions)
        projected = Post._base_manager.filter(pk__in=ids).annotate(
            can_read=permission_expression(Post, "read", actor=actor),
            can_caveated_read=permission_expression(Post, "caveated_read", actor=actor),
        )
        return list(handle.attach(projected).values_list("pk", "can_read", "can_caveated_read"))

    def evaluator_list() -> object:
        with evaluator_scope():
            return scope()

    def evaluator_page(subject: SubjectRef) -> object:
        with evaluator_scope():
            rows = Post.objects.with_actor(subject)
            return list(rows.values_list("pk", flat=True)), rows.all().count()

    def bulk_check() -> object:
        return read.check_many(backend=backend, items=bulk, using="default")

    original = read._Operation.statement
    original_probe = Compiler.decide_native_render
    original_holds = Compiler.holds

    def track_probe(probe_times):
        def timed_probe(self, render):
            def timed_render():
                started = perf_counter()
                try:
                    return render()
                finally:
                    probe_times.append((perf_counter() - started) * 1000)

            return original_probe(self, timed_render)

        return timed_probe

    def track_holds(holds_calls):
        def counted_holds(self, *args, **kwargs):
            holds_calls.append(1)
            return original_holds(self, *args, **kwargs)

        return counted_holds

    results = {}
    for name, fn in (
        ("pk", point),
        ("pk_in_50", many),
        ("ordinary_decided_list", scope),
        ("evaluator_decided_list", evaluator_list),
        ("evaluator_decided_page", lambda: evaluator_page(actor)),
        (
            "admin_decided_list",
            lambda: list(Post.objects.with_actor(admin).values_list("pk", flat=True)),
        ),
        ("admin_decided_page", lambda: evaluator_page(admin)),
        ("caveated_scope", caveated_scope),
        ("multi_action_scope", multi_action_scope),
        ("multi_action_caveated", multi_action_caveated),
        ("bulk_300", bulk_check),
    ):
        phases = {}
        for phase in ("before_no_cache", "after_cache"):
            builds = [0]
            probe_times: list[float] = []
            holds_calls: list[int] = []

            def counted(self, key, verdicts, build, *, phase=phase, builds=builds, **kwargs):
                def tracked():
                    builds[0] += 1
                    return build()

                if phase == "before_no_cache":
                    kwargs["cache"] = False
                return original(self, key, verdicts, tracked, **kwargs)

            read.reset()
            with monkeypatch.context() as patcher:
                patcher.setattr(read._Operation, "statement", counted)
                patcher.setattr(Compiler, "decide_native_render", track_probe(probe_times))
                patcher.setattr(Compiler, "holds", track_holds(holds_calls))
                requests = []
                for _ in range(2):
                    before = builds[0]
                    probe_times.clear()
                    holds_calls.clear()
                    started = perf_counter()
                    with CaptureQueriesContext(connection) as queries:
                        fn()
                    elapsed_ms = (perf_counter() - started) * 1000
                    db_ms = sum(float(query["time"]) * 1000 for query in queries)
                    requests.append(
                        {
                            "compiles": builds[0] - before,
                            "ms": round(elapsed_ms, 2),
                            "python_ms": round(max(0.0, elapsed_ms - db_ms), 2),
                            "db_ms": round(db_ms, 2),
                            "queries": len(queries),
                            "probe_renders": len(probe_times),
                            "holds": len(holds_calls),
                            "probe_render_ms": round(sum(probe_times), 2),
                            "max_sql_bytes": max(
                                (len(query["sql"].encode()) for query in queries), default=0
                            ),
                        }
                    )
            stats = read.statement_cache_stats()
            phases[phase] = {
                "requests": requests,
                "hit_ratio": round(stats["hits"] / max(1, stats["hits"] + stats["misses"]), 3),
                "cache_bytes": stats["bytes"],
                "hits": stats["hits"],
                "misses": stats["misses"],
            }
        results[name] = phases
        assert phases["after_cache"]["requests"][1]["compiles"] == 0, name
    for prefix in ("evaluator", "admin"):
        list_queries = results[f"{prefix}_decided_list"]["after_cache"]["requests"][1]["queries"]
        page_queries = results[f"{prefix}_decided_page"]["after_cache"]["requests"][1]["queries"]
        assert page_queries == list_queries + 1  # count uses the evaluator's preparation
        for suffix in ("list", "page"):
            warm = results[f"{prefix}_decided_{suffix}"]["after_cache"]["requests"][1]
            assert warm["probe_renders"] == 0
            assert warm["compiles"] == 0
    assert results["caveated_scope"]["after_cache"]["requests"][1]["probe_renders"] == 0
    assert results["multi_action_scope"]["after_cache"]["requests"][1]["queries"] == 1
    assert results["multi_action_scope"]["after_cache"]["requests"][1]["probe_renders"] == 1
    fallback = results["multi_action_caveated"]
    assert fallback["before_no_cache"]["requests"][1]["compiles"] == 2
    assert fallback["after_cache"]["requests"][1]["compiles"] == 0
    assert fallback["after_cache"]["requests"][1]["probe_renders"] == 0
    other = SubjectRef.of("auth/user", "other")
    with sudo(reason="cross-actor statement-cache benchmark fixture"):
        other_folder = Folder.objects.create(name="other-allowed")
        other_posts = Post._base_manager.bulk_create(
            [Post(title=f"other-post-{number}", folder=other_folder) for number in range(50)]
        )
    backend.write_relationships([RelationshipTuple(to_object_ref(other_folder), "viewer", other)])

    def visible(subject: SubjectRef) -> set[int]:
        return set(Post.objects.with_actor(subject).values_list("pk", flat=True))

    read.reset()
    assert visible(actor) == {post.pk for post in posts}
    before = read.statement_cache_stats()
    cached = visible(other)
    after = read.statement_cache_stats()
    assert cached == {post.pk for post in other_posts}
    assert cached.isdisjoint({post.pk for post in posts})
    assert after["hits"] == before["hits"] + 1
    assert after["misses"] == before["misses"]
    with patch.object(read._Operation, "statement", autospec=True) as uncached:
        uncached.side_effect = lambda self, key, verdicts, build, **kwargs: original(
            self, key, verdicts, build, **{**kwargs, "cache": False}
        )
        assert visible(other) == cached
    results["cross_actor"] = {"cached_hits": after["hits"] - before["hits"], "rows": len(cached)}
    print("STATEMENT_CACHE_BENCH " + json.dumps(results, sort_keys=True))
