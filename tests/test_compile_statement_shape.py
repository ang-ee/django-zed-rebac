"""Statement-local projections and owner names preserve compiled reads."""

from __future__ import annotations

import re
from statistics import median

import pytest
from django.db import connection, models
from django.db.models.functions import Cast
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.codec import identity_codec
from rebac.compile import predicate, read
from rebac.compile.predicate import Compiler
from rebac.managers import share_scoped_definitions
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]

SCHEMA = """
definition auth/user {}
definition auth/group { relation member: auth/user | auth/group#member }
definition test/role { relation member: auth/user }
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/group#member
    relation admin: test/role // rebac:const=admin
    relation auditor: test/role // rebac:const=auditor
    permission read = ((viewer + admin->member) + auditor->member) + parent->read
    permission p1 = viewer + admin->member
    permission p2 = viewer + admin->member
    permission p3 = viewer + admin->member
    permission p4 = viewer + admin->member
    permission p5 = viewer + admin->member
    permission p6 = viewer + admin->member
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation admin: test/role // rebac:const=admin
    relation auditor: test/role // rebac:const=auditor
    permission read = ((((((folder->p1 + folder->p2) + folder->p3) + folder->p4) + folder->p5) + folder->p6) + admin->member) - auditor->member
    permission view = folder->read + admin->member
}
"""


@pytest.fixture
def statement_rows(monkeypatch):
    monkeypatch.setattr(read, "_scope_decide", lambda *_args, **_kwargs: False)
    local = install_schema(SCHEMA)
    actor = SubjectRef.of("auth/user", "shape-reader")
    with sudo(reason="statement shape fixture"):
        root = Folder.objects.create(name="shape root")
        folder = Folder.objects.create(name="shape folder", parent=root)
        post = Post.objects.create(title="shape post", folder=folder)
    group = ObjectRef("auth/group", "shape-group")
    local.write_relationships(
        [
            RelationshipTuple(group, "member", actor),
            RelationshipTuple(to_object_ref(root), "viewer", SubjectRef(group, "member")),
            RelationshipTuple(ObjectRef("test/role", "admin"), "member", actor),
        ]
    )
    return actor, post


def _scope_query(actor: SubjectRef):
    read.reset()
    return Post.objects.with_actor(actor).with_action("read").scoped()


def _planning_ms(queryset) -> tuple[float, float] | None:
    if connection.vendor != "postgresql":
        return None
    sql, params = queryset.query.sql_with_params()
    samples = []
    with connection.cursor() as cursor:
        for _ in range(3):
            cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
            plan = cursor.fetchone()[0][0]
            samples.append((plan["Planning Time"], plan["Execution Time"]))
    return round(median(item[0] for item in samples), 3), round(
        median(item[1] for item in samples), 3
    )


def test_native_probe_preserves_another_statements_compiled_id_set(statement_rows, monkeypatch):
    actor, _post = statement_rows
    cached_set = predicate._Compiled(Post._base_manager.order_by().values("pk"))
    other_statement = Post._base_manager.filter(pk__in=cached_set)
    other_statement.query.get_compiler(using=connection.alias).as_sql()
    cached_sql = cached_set._sql.copy()
    assert cached_sql

    original = Compiler.decide_native_render
    probes = [0]

    def counted(self, render):
        probes[0] += 1
        return original(self, render)

    monkeypatch.setattr(Compiler, "decide_native_render", counted)
    _scope_query(actor).query.get_compiler(using=connection.alias).as_sql()
    assert probes[0] > 0
    assert cached_set._sql == cached_sql


def test_native_guard_renders_once_per_boundary(statement_rows, monkeypatch):
    actor, post = statement_rows
    original_recursion = Compiler.has_recursion

    def unhoisted_native(self, model, identity, ref, resource_type):
        return identity_codec(model, identity).to_column(ref)

    def unhoisted_const(self, key):
        return key == ("test/role", "member") or original_recursion(self, key)

    with monkeypatch.context() as before:
        before.setattr(Compiler, "_native_reference", unhoisted_native)
        before.setattr(Compiler, "has_recursion", unhoisted_const)
        original_query = _scope_query(actor)
        original_sql = str(original_query.query)
        original_plan = _planning_ms(original_query)
    with monkeypatch.context() as native_only:
        native_only.setattr(Compiler, "has_recursion", unhoisted_const)
        native_query = _scope_query(actor)
        native_sql = str(native_query.query)
        native_plan = _planning_ms(native_query)

    class UnguardedCodec:
        def __init__(self, codec):
            self.codec = codec

        def __getattr__(self, name):
            return getattr(self.codec, name)

        def to_column(self, expr):
            return Cast(expr, self.codec.field)

    original_codec = predicate.identity_codec
    with monkeypatch.context() as guardless:
        guardless.setattr(Compiler, "has_recursion", unhoisted_const)
        guardless.setattr(
            predicate,
            "identity_codec",
            lambda model, identity=None: UnguardedCodec(original_codec(model, identity)),
        )
        guardless_query = _scope_query(actor)
        guardless_sql = str(guardless_query.query)
        guardless_plan = _planning_ms(guardless_query)
    final_query = _scope_query(actor)
    final_sql = str(final_query.query)
    final_plan = _planning_ms(final_query)

    # Reused boundaries share their guard; single-use boundaries stay inline.
    guard = "^(0|[1-9][0-9]*|-[1-9][0-9]*)$"
    assert original_sql.count(guard) > native_sql.count(guard)
    assert native_sql.count(guard) == final_sql.count(guard)
    assert native_sql.count("test/role") > final_sql.count("test/role")
    assert len(re.findall(r'"rebacnative_[^\"]+" AS ', native_sql)) >= 1
    assert guardless_sql.count(guard) == 0
    assert len(final_sql.encode()) <= 14_210
    assert len(re.findall(r'"rebac_const_[^\"]+" AS \(', final_sql)) == 2
    assert list(
        Post.objects.with_actor(actor).with_action("read").values_list("pk", flat=True)
    ) == [post.pk]
    print(
        "STATEMENT_SHAPE "
        + repr(
            {
                "original_bytes": len(original_sql.encode()),
                "native_bytes": len(native_sql.encode()),
                "final_bytes": len(final_sql.encode()),
                "guard": (
                    original_sql.count(guard),
                    native_sql.count(guard),
                    final_sql.count(guard),
                ),
                "const": (
                    original_sql.count("test/role"),
                    native_sql.count("test/role"),
                    final_sql.count("test/role"),
                ),
                "pg_plan_execution_ms": (original_plan, native_plan, final_plan),
                "guardless_bytes": len(guardless_sql.encode()),
                "guardless_pg_plan_execution_ms": guardless_plan,
                "wire_key_reference_bytes": sum(
                    len(match.group().encode())
                    for match in re.finditer(r'"[^\"]+"\."(?:resource_id|subject_id)"', native_sql)
                ),
            }
        )
    )


def test_fixed_object_renders_once_per_bound(statement_rows, monkeypatch):
    actor, _post = statement_rows
    original_recursion = Compiler.has_recursion

    with monkeypatch.context() as unhoisted:
        unhoisted.setattr(
            Compiler,
            "has_recursion",
            lambda self, key: key == ("test/role", "member") or original_recursion(self, key),
        )
        before = str(_scope_query(actor).query)
    after = str(_scope_query(actor).query)

    assert before.count("test/role") == 8
    assert after.count("test/role") == 2
    assert len(after.encode()) < len(before.encode())
    assert len(re.findall(r'"rebac_const_[^\"]+" AS \(', after)) == 2


def test_attached_and_union_owner_names_are_repeatable(statement_rows, monkeypatch):
    actor, post = statement_rows

    def attached() -> str:
        read.reset()
        handle = Post.objects.with_actor(actor).scope_cte(actions=("read", "view"))
        base = Post._base_manager.filter(pk__in=handle.queryset().values("pk"))
        return str(handle.attach(base).query)

    def union() -> str:
        read.reset()
        branches = [
            Post.objects.with_actor(actor).with_action(action).scoped().values("pk")
            for action in ("read", "view")
        ]
        plain = [
            models.QuerySet(model=Post, query=branch.query.clone(), using=branch.db)
            for branch in branches
        ]
        return str(share_scoped_definitions(plain[0].union(plain[1], all=True)).query)

    attached_sql = attached()
    union_sql = union()
    assert attached_sql == attached()
    assert union_sql == union()

    original_init = Compiler.__init__

    def wide_owner(self, *args, **kwargs):
        if kwargs.get("cte_owner") == "0":
            kwargs["cte_owner"] = "0123456789abcdef"
        original_init(self, *args, **kwargs)

    with monkeypatch.context() as prior_width:
        prior_width.setattr(Compiler, "__init__", wide_owner)
        wide_attached = attached()
        wide_union = union()
    assert len(wide_attached.encode()) > len(attached_sql.encode())
    assert len(wide_union.encode()) > len(union_sql.encode())
    print(
        "OWNER_WIDTH "
        + repr(
            {
                "attached": (len(wide_attached.encode()), len(attached_sql.encode())),
                "union": (len(wide_union.encode()), len(union_sql.encode())),
            }
        )
    )
    assert list(Post.objects.with_actor(actor).values_list("pk", flat=True)) == [post.pk]


def test_const_projection_rechecks_expiration(monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    import rebac.clock

    monkeypatch.setattr(read, "_scope_decide", lambda *_args, **_kwargs: False)
    local = install_schema("""
        use expiration
        definition auth/user {}
        definition test/role { relation member: auth/user with expiration }
        definition blog/post {
            relation admin: test/role // rebac:const=admin
            relation auditor: test/role // rebac:const=auditor
            permission read = admin->member - auditor->member
        }
    """)
    actor = SubjectRef.of("auth/user", "expiring-admin")
    with sudo(reason="expiring const fixture"):
        post = Post.objects.create(title="expiring admin")
    now = timezone.now()
    deadline = now + timedelta(minutes=1)
    local.write_relationships(
        [RelationshipTuple(ObjectRef("test/role", "admin"), "member", actor, expires_at=deadline)]
    )
    monkeypatch.setattr(rebac.clock, "application_now", lambda: now)
    prepared = Post.objects.with_actor(actor).scoped()
    assert list(prepared.values_list("pk", flat=True)) == [post.pk]
    monkeypatch.setattr(rebac.clock, "application_now", lambda: deadline)
    assert not prepared.all().exists()


def test_point_recursion_names_are_stable_across_compiles():
    local = install_schema("""
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user
            relation parent: blog/post
            permission read = viewer + parent->read
        }
    """)
    actor = SubjectRef.of("auth/user", "recursive-reader")
    with sudo(reason="recursive statement fixture"):
        root = Post.objects.create(title="root")
        child = Post.objects.create(title="child")
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(root), "viewer", actor),
            RelationshipTuple(
                to_object_ref(child), "parent", SubjectRef.of("blog/post", str(root.pk))
            ),
        ]
    )

    def statement():
        read.reset()
        with CaptureQueriesContext(connection) as queries:
            assert local.check_access(
                subject=actor, action="read", resource=to_object_ref(child)
            ).allowed
        [sql] = [query["sql"] for query in queries if "rebac_reached_" in query["sql"]]
        return sql

    assert statement() == statement()
