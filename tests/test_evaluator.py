"""Tests for ``rebac.evaluator.PermissionEvaluator``.

Covers cache hit/miss, conditional bypass, LRU eviction, scope nesting,
async-task isolation, and stats reporting.
"""

from __future__ import annotations

from contextvars import copy_context
from typing import Any

from rebac import (
    Backend,
    CheckResult,
    ObjectRef,
    PermissionEvaluator,
    SubjectRef,
    current_evaluator,
    evaluator_scope,
)


class _StubBackend(Backend):
    """Counts calls so tests can assert "exactly one backend hit".

    Subclasses :class:`Backend` so it satisfies the evaluator's type
    contract; only ``check_access`` / ``accessible`` are exercised, the
    rest raise.
    """

    kind = "local"

    def __init__(
        self,
        *,
        check_result: CheckResult | None = None,
        accessible_ids: tuple[str, ...] = (),
    ) -> None:
        self.check_calls = 0
        self.accessible_calls = 0
        # ``check_result or CheckResult.has()`` would coerce a falsy
        # conditional result (allowed=False) back to .has() because
        # ``CheckResult.__bool__`` returns ``allowed``. Use explicit
        # ``is None`` instead.
        self._check_result = check_result if check_result is not None else CheckResult.has()
        self._accessible_ids = accessible_ids

    def check_access(self, **_: Any) -> CheckResult:
        self.check_calls += 1
        return self._check_result

    def accessible(self, **_: Any) -> tuple[str, ...]:
        self.accessible_calls += 1
        return self._accessible_ids

    def lookup_subjects(self, **_: Any) -> list[SubjectRef]:
        raise NotImplementedError

    def write_relationships(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError

    def delete_relationships(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError

    def delete_relationship(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError

    def schema(self) -> Any:
        raise NotImplementedError


# ---------- ContextVar machinery ----------


def test_current_evaluator_is_none_outside_scope():
    assert current_evaluator() is None


def test_evaluator_scope_yields_fresh_instance():
    with evaluator_scope() as e1:
        assert current_evaluator() is e1
        with evaluator_scope() as e2:
            assert current_evaluator() is e2
            assert e2 is not e1
        # Nested scope torn down; outer scope restored.
        assert current_evaluator() is e1
    assert current_evaluator() is None


def test_evaluator_scope_install_custom_instance():
    custom = PermissionEvaluator(max_size=5)
    with evaluator_scope(custom) as e:
        assert e is custom
        assert current_evaluator() is custom


# ---------- Check cache hit/miss ----------


def test_check_dedups_same_args():
    backend = _StubBackend()
    subj = SubjectRef.of("auth/user", "1")
    res = ObjectRef("blog/post", "p1")
    ev = PermissionEvaluator()
    ev.check(backend, subject=subj, action="read", resource=res)
    ev.check(backend, subject=subj, action="read", resource=res)
    assert backend.check_calls == 1


def test_check_different_actions_miss():
    backend = _StubBackend()
    subj = SubjectRef.of("auth/user", "1")
    res = ObjectRef("blog/post", "p1")
    ev = PermissionEvaluator()
    ev.check(backend, subject=subj, action="read", resource=res)
    ev.check(backend, subject=subj, action="write", resource=res)
    assert backend.check_calls == 2


def test_check_different_subjects_miss():
    backend = _StubBackend()
    res = ObjectRef("blog/post", "p1")
    ev = PermissionEvaluator()
    ev.check(backend, subject=SubjectRef.of("auth/user", "1"), action="read", resource=res)
    ev.check(backend, subject=SubjectRef.of("auth/user", "2"), action="read", resource=res)
    assert backend.check_calls == 2


def test_check_conditional_bypasses_cache():
    """Conditional results never cached — the next call may resolve."""
    backend = _StubBackend(check_result=CheckResult.conditional(missing=("ip",)))
    subj = SubjectRef.of("auth/user", "1")
    res = ObjectRef("blog/post", "p1")
    ev = PermissionEvaluator()
    ev.check(backend, subject=subj, action="read", resource=res)
    ev.check(backend, subject=subj, action="read", resource=res)
    assert backend.check_calls == 2
    assert ev.stats()["check_entries"] == 0


def test_check_with_same_context_hits_cache():
    """Context is part of the cache key — same context = same key = hit."""
    backend = _StubBackend()
    subj = SubjectRef.of("auth/user", "1")
    res = ObjectRef("blog/post", "p1")
    ev = PermissionEvaluator()
    ev.check(backend, subject=subj, action="read", resource=res, context={"ip": "1.1.1.1"})
    ev.check(backend, subject=subj, action="read", resource=res, context={"ip": "1.1.1.1"})
    assert backend.check_calls == 1


def test_check_with_different_context_misses():
    """Different context dicts hash to different keys and miss."""
    backend = _StubBackend()
    subj = SubjectRef.of("auth/user", "1")
    res = ObjectRef("blog/post", "p1")
    ev = PermissionEvaluator()
    ev.check(backend, subject=subj, action="read", resource=res, context={"ip": "1.1.1.1"})
    ev.check(backend, subject=subj, action="read", resource=res, context={"ip": "2.2.2.2"})
    assert backend.check_calls == 2


# ---------- Accessible cache hit/miss ----------


def test_accessible_dedups_same_args():
    backend = _StubBackend(accessible_ids=("a", "b", "c"))
    subj = SubjectRef.of("auth/user", "1")
    ev = PermissionEvaluator()
    out1 = ev.accessible(backend, subject=subj, action="read", resource_type="blog/post")
    out2 = ev.accessible(backend, subject=subj, action="read", resource_type="blog/post")
    assert backend.accessible_calls == 1
    assert out1 == out2 == ("a", "b", "c")


def test_accessible_different_resource_type_miss():
    backend = _StubBackend(accessible_ids=())
    subj = SubjectRef.of("auth/user", "1")
    ev = PermissionEvaluator()
    ev.accessible(backend, subject=subj, action="read", resource_type="blog/post")
    ev.accessible(backend, subject=subj, action="read", resource_type="drive/file")
    assert backend.accessible_calls == 2


# ---------- LRU eviction ----------


def test_lru_eviction_drops_oldest_after_max_size():
    backend = _StubBackend()
    ev = PermissionEvaluator(max_size=3)
    for i in range(5):
        ev.check(
            backend,
            subject=SubjectRef.of("auth/user", str(i)),
            action="read",
            resource=ObjectRef("blog/post", "p"),
        )
    # 3 fit, 2 evicted.
    assert ev.stats()["check_entries"] == 3
    # Re-checking the most-recent 3 keys should hit cache; re-checking
    # the first 2 should miss.
    for i in (2, 3, 4):
        ev.check(
            backend,
            subject=SubjectRef.of("auth/user", str(i)),
            action="read",
            resource=ObjectRef("blog/post", "p"),
        )
    assert backend.check_calls == 5  # no new misses for cached keys
    for i in (0, 1):
        ev.check(
            backend,
            subject=SubjectRef.of("auth/user", str(i)),
            action="read",
            resource=ObjectRef("blog/post", "p"),
        )
    # +2 evicted-key misses.
    assert backend.check_calls == 7


def test_lru_move_to_end_on_hit():
    """Accessing a cached entry promotes it; eviction picks the new LRU."""
    backend = _StubBackend()
    ev = PermissionEvaluator(max_size=3)
    keys = [
        ObjectRef("blog/post", "a"),
        ObjectRef("blog/post", "b"),
        ObjectRef("blog/post", "c"),
    ]
    subj = SubjectRef.of("auth/user", "1")
    for k in keys:
        ev.check(backend, subject=subj, action="read", resource=k)
    # Touch 'a' so 'b' becomes the new LRU.
    ev.check(backend, subject=subj, action="read", resource=keys[0])
    # Insert a 4th key — 'b' evicts, 'a' / 'c' / 'd' survive.
    ev.check(backend, subject=subj, action="read", resource=ObjectRef("blog/post", "d"))
    # 'a' still cached, 'b' evicted, 'c' / 'd' cached.
    pre = backend.check_calls
    ev.check(backend, subject=subj, action="read", resource=keys[0])  # hit
    assert backend.check_calls == pre
    ev.check(backend, subject=subj, action="read", resource=keys[1])  # miss
    assert backend.check_calls == pre + 1


def test_eviction_shared_between_check_and_accessible():
    """Total entries across BOTH caches counts against the limit."""
    backend = _StubBackend(accessible_ids=())
    ev = PermissionEvaluator(max_size=2)
    ev.check(
        backend,
        subject=SubjectRef.of("auth/user", "1"),
        action="read",
        resource=ObjectRef("blog/post", "p"),
    )
    ev.accessible(
        backend, subject=SubjectRef.of("auth/user", "1"), action="read", resource_type="x"
    )
    ev.accessible(
        backend, subject=SubjectRef.of("auth/user", "2"), action="read", resource_type="x"
    )
    # 3 inserted, 2 max — one evicted.
    stats = ev.stats()
    assert stats["check_entries"] + stats["accessible_entries"] == 2


# ---------- Invalidation ----------


def test_invalidate_drops_all_entries():
    backend = _StubBackend(accessible_ids=())
    ev = PermissionEvaluator()
    ev.check(
        backend,
        subject=SubjectRef.of("auth/user", "1"),
        action="read",
        resource=ObjectRef("blog/post", "p"),
    )
    ev.accessible(
        backend,
        subject=SubjectRef.of("auth/user", "1"),
        action="read",
        resource_type="blog/post",
    )
    assert ev.stats()["check_entries"] == 1
    assert ev.stats()["accessible_entries"] == 1
    ev.invalidate()
    assert ev.stats()["check_entries"] == 0
    assert ev.stats()["accessible_entries"] == 0


# ---------- Scope isolation ----------


def test_evaluator_scopes_isolated_across_copied_contexts():
    """`copy_context().run(...)` mirrors `asyncio.create_task` semantics —
    each task inherits the parent ContextVar at fork time but can shadow
    it without leaking back to the parent."""
    backend = _StubBackend()
    subj = SubjectRef.of("auth/user", "1")
    res = ObjectRef("blog/post", "p1")
    collected: list[PermissionEvaluator] = []

    def child_runs_in_own_scope() -> None:
        with evaluator_scope() as ev:
            ev.check(backend, subject=subj, action="read", resource=res)
            # Hold a reference so id() comparison below is meaningful
            # (CPython recycles object ids after GC).
            collected.append(ev)

    with evaluator_scope() as parent:
        parent.check(backend, subject=subj, action="read", resource=res)
        ctx1 = copy_context()
        ctx2 = copy_context()
        ctx1.run(child_runs_in_own_scope)
        ctx2.run(child_runs_in_own_scope)
        assert len(collected) == 2
        # Each fork installs its own evaluator instance distinct from
        # the parent and from each other.
        assert collected[0] is not parent
        assert collected[1] is not parent
        assert collected[0] is not collected[1]
        # Parent's cache stays intact regardless of what the children did.
        assert parent.stats()["check_entries"] == 1
