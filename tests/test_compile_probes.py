"""Internal probes own their SQL boundary during outer statement compilation."""

import pytest
from django.db import connection
from django.db.models import Exists, OuterRef
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, PermissionDenied, RelationshipTuple, SubjectRef, share_id_sets, sudo
from rebac.compile import ctes, read
from rebac.testing import install_schema
from tests.testapp.models import Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]

SCHEMAS = {
    "group": """
        definition auth/user {}
        definition auth/group { relation member: auth/user }
        definition blog/post {
            relation viewer: auth/user | auth/group#member
            permission read = viewer
            permission read__title = viewer
        }
    """,
    "role": """
        definition auth/user {}
        definition org/role {
            relation direct: auth/user
            relation includes: org/role
            permission member = direct + includes->member
        }
        definition blog/post {
            relation viewer: auth/user
            relation admin: org/role // rebac:const=admin
            permission read = viewer + admin->member
            permission read__title = viewer + admin->member
        }
    """,
    "role_group": """
        definition auth/user {}
        definition auth/group { relation member: auth/user }
        definition org/role {
            relation direct: auth/user | auth/group#member
            relation includes: org/role
            permission member = direct + includes->member
        }
        definition blog/post {
            relation viewer: auth/user | auth/group#member
            relation admin: org/role // rebac:const=admin
            permission read = viewer + admin->member
            permission read__title = viewer + admin->member
        }
    """,
}


@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("schema", sorted(SCHEMAS))
@pytest.mark.parametrize("root", ["shared", "plain", "rebac", "sudo"])
@pytest.mark.parametrize("render_only", [False, True])
def test_gated_subquery_probe_owns_its_ctes(schema, root, render_only):
    local = install_schema(SCHEMAS[schema])
    actor = SubjectRef.of("auth/user", "1")
    with sudo(reason="probe fixture"):
        post = Post.objects.create(title="secret plan")
    grants = [RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "viewer", actor)]
    allowed = schema == "role_group"
    if allowed:
        grants += [
            RelationshipTuple(ObjectRef("auth/group", "g"), "member", actor),
            RelationshipTuple(
                ObjectRef("org/role", "admin"), "includes", SubjectRef.of("org/role", "child")
            ),
            RelationshipTuple(
                ObjectRef("org/role", "child"), "direct", SubjectRef.of("auth/group", "g", "member")
            ),
        ]
    local.write_relationships(grants)
    read.reset()
    inner = Exists(
        Post.objects.with_actor(actor).filter(pk=OuterRef("pk"), title__startswith="secret")
    )
    if root == "rebac":
        rows = Post.objects.with_actor(actor).filter(inner)
    elif root == "sudo":
        rows = Post.objects.sudo(reason="probe outer").filter(inner)
    else:
        rows = Post._base_manager.filter(inner)
        if root == "shared":
            rows = share_id_sets(rows)

    def run():
        return str(rows.query) if render_only else list(rows.values_list("pk", flat=True))

    with CaptureQueriesContext(connection) as captured:
        if allowed:
            result = run()
            assert result if render_only else result == [post.pk]
        else:
            # A per-row grant cannot authorize filtering on a gated column.
            with pytest.raises(PermissionDenied):
                run()
    assert ctes._current.get() is None
    probes = [q["sql"] for q in captured if "rebac_ids_" in q["sql"]]
    assert probes
    assert all("WITH RECURSIVE" in sql for sql in probes)


def test_lazy_projection_witness_executes_inside_its_own_root():
    from rebac import evaluator_scope, permission_expression

    local = install_schema(SCHEMAS["role_group"])
    actor = SubjectRef.of("auth/user", "1")
    with sudo(reason="lazy witness fixture"):
        post = Post.objects.create(title="visible")
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("org/role", "admin"), "direct", actor),
        ]
    )
    rows = share_id_sets(Post._base_manager.all()).annotate(
        allowed=permission_expression(Post, "read", actor=actor)
    )
    with evaluator_scope(), CaptureQueriesContext(connection) as captured:
        assert list(rows.values_list("pk", "allowed")) == [(post.pk, True)]
    # Both the witness and the outer SELECT have complete definitions.
    statements = [q["sql"] for q in captured if "rebac_ids_" in q["sql"]]
    assert len(statements) == 2
    assert all(sql.startswith("WITH RECURSIVE") for sql in statements)
    assert ctes._current.get() is None


@pytest.mark.parametrize("bulk", [False, True])
def test_boolean_point_probe_has_its_own_compile_clock(monkeypatch, bulk):
    from dataclasses import replace
    from datetime import timedelta

    from django.utils import timezone

    local = install_schema(SCHEMAS["group"])
    actor = SubjectRef.of("auth/user", "1")
    before = timezone.now()
    deadline = before + timedelta(seconds=1)
    operation = read._Operation.begin(local, actor, "default")
    operation = replace(
        operation,
        policy=replace(operation.policy, selected_at=before, caveat_deadlines=(deadline,)),
    )
    monkeypatch.setattr(read, "_point_statement", lambda *args: True)
    monkeypatch.setattr("rebac.clock.application_now", lambda: before)
    key = ("blog/post", "read")
    with ctes.collecting() as outer:
        assert outer.now == before
        monkeypatch.setattr("rebac.clock.application_now", lambda: deadline)
        if bulk:
            assert read._points([(operation, key, "1")], "lower", [None]) == [False]
        else:
            assert not read._point(operation, key, "1", "lower")
        assert ctes.statement_now() == before
        assert not outer.definitions
    assert ctes._current.get() is None
