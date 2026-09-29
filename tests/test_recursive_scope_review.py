"""Release-review regressions for dispatch analysis and bounded SQL growth."""

from dataclasses import FrozenInstanceError
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import (
    PermissionDepthExceeded,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from rebac.backends.local_query import LocalQueryScope, UnsupportedScope, _CompileContext
from rebac.backends.local_recursive import (
    DepthCheckedPredicate,
    RecursiveQueryScope,
    reaches_self_arrow,
)
from rebac.schema import parse_zed
from rebac.schema.introspection import accessible_is_exact, dispatch_edges, permission_sources
from tests.test_recursive_queryscope import ACTOR, OUTSIDER, grant, no_enumeration
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db

NESTED_GROUPS = """
definition auth/user {}
definition auth/group { relation member: auth/user | auth/group#member }
definition blog/folder {
    relation viewer: auth/user | auth/group#member
    permission read = viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
"""
COMPOSED = """
definition auth/user {}
definition blog/folder {
    relation member: auth/user
    relation reader: auth/user
    relation includes: blog/folder
    relation parent: blog/folder
    permission effective_member = member + includes->effective_member
    permission read = (reader + effective_member) + parent->read
}
"""


@pytest.fixture(params=["denormalized", "registry"])
def active(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        yield backend()
        reset_backend()


def test_dispatch_edges_keep_kind_and_arrow_occurrences():
    schema = parse_zed(
        COMPOSED.replace(
            "(reader + effective_member) + parent->read",
            "(effective_member + effective_member) + parent->read",
        )
    )
    edges = dispatch_edges(schema, "blog/folder", "read")
    assert [(e.resource_type, e.action, e.via, e.is_arrow) for e in edges] == [
        ("blog/folder", "effective_member", "includes", True),
        ("blog/folder", "effective_member", "includes", True),
        ("blog/folder", "read", "parent", True),
    ]
    assert permission_sources(schema, "blog/folder", "read").arrows == {
        ("includes", "effective_member"),
        ("parent", "read"),
    }
    group_edges = dispatch_edges(parse_zed(NESTED_GROUPS), "auth/group", "member")
    assert [(e.resource_type, e.action, e.is_arrow) for e in group_edges] == [
        ("auth/group", "member", False)
    ]
    assert dispatch_edges(schema, "missing/type", "read") == ()
    assert dispatch_edges(schema, "blog/folder", "missing") == ()


def test_cycle_classification_requires_an_arrow_inside_the_cycle():
    nested = parse_zed(NESTED_GROUPS)
    assert accessible_is_exact(nested)
    assert not reaches_self_arrow(nested, "blog/post", "read")
    assert not accessible_is_exact(parse_zed(COMPOSED))
    assert reaches_self_arrow(parse_zed(COMPOSED), "blog/folder", "read")
    # An acyclic incoming arrow doesn't turn the nested-group loop into an
    # arrow cycle. Mutual arrows across two definitions do form such a cycle.
    mutual = parse_zed("""
        definition one/type { relation next: two/type permission read = next->read }
        definition two/type { relation next: one/type permission read = next->read }
    """)
    assert not accessible_is_exact(mutual)


def test_nested_groups_keep_v020_query_cost_and_depth_errors(active):
    active.set_schema(parse_zed(NESTED_GROUPS))
    with sudo(reason="nested group query-cost regression"):
        folder = Folder.objects.create(name="group")
        post = Post.objects.create(title="group", folder=folder)
    groups = [SubjectRef.of("auth/group", str(i), "member") for i in range(2)]
    active.write_relationships(
        [
            RelationshipTuple(to_object_ref(folder), "viewer", groups[0]),
            RelationshipTuple(groups[0].object, "member", groups[1]),
            RelationshipTuple(groups[1].object, "member", ACTOR),
        ]
    )
    assert accessible_is_exact(active.schema())
    with (
        patch.object(active, "_eval_permission_on", side_effect=AssertionError("per-target walk")),
        CaptureQueriesContext(connection) as captured,
    ):
        assert active.check_access(
            subject=ACTOR, action="read", resource=to_object_ref(post)
        ).allowed
    # Measured on the saved v0.20.0 checkout in both stores: twelve tuple
    # enumeration statements and one bounded field-path EXISTS, thirteen total.
    assert len(captured) == 13
    assert sum('FROM "testapp_post"' in query["sql"] for query in captured) == 1
    assert captured[-1]["sql"].startswith('SELECT 1 AS "a" FROM "testapp_post"')
    with override_settings(REBAC_DEPTH_LIMIT=1):
        for evaluate in (
            lambda: active.check_access(subject=ACTOR, action="read", resource=to_object_ref(post)),
            lambda: list(
                active.accessible(subject=ACTOR, action="read", resource_type="blog/post")
            ),
        ):
            with pytest.raises(PermissionDepthExceeded):
                evaluate()


@pytest.mark.parametrize(
    "body",
    [
        "(reader + parent->read) + includes->read",
        "(reader + parent->read) + parent->read",
        "reader + (alias + alias)",
    ],
)
def test_multiple_self_arrows_refuse_before_expansion_and_fall_back(active, body):
    active.set_schema(
        parse_zed(f"""
        definition auth/user {{}}
        definition blog/folder {{
            relation reader: auth/user
            relation parent: blog/folder
            relation includes: blog/folder
            permission alias = parent->read
            permission read = {body}
        }}
    """)
    )
    with patch.object(RecursiveQueryScope, "relation", side_effect=AssertionError("unrolled")):
        with pytest.raises(UnsupportedScope, match="Multiple self-arrows"):
            LocalQueryScope(active, ACTOR, "default").predicate(Folder, "read", "blog/folder")
        assert (
            active.queryset_filter(model=Folder, subject=ACTOR, action="read", using="default")
            is None
        )
    with sudo(reason="multi-arrow evaluator fallback"):
        row = Folder.objects.create(name="direct")
    grant(active, row, "reader")
    with patch.object(active, "accessible", wraps=active.accessible) as enumeration:
        assert list(Folder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [row.pk]
    assert enumeration.called


def test_composed_recursions_remain_supported_at_default_bound(active):
    active.set_schema(parse_zed(COMPOSED))
    with sudo(reason="composed recursions"):
        member = Folder.objects.create(name="member")
        role = Folder.objects.create(name="included role")
        leaf = Folder.objects.create(name="child folder")
    grant(active, member, "member")
    active.write_relationships(
        [
            RelationshipTuple(
                to_object_ref(role), "includes", SubjectRef.of("blog/folder", str(member.pk))
            ),
            RelationshipTuple(
                to_object_ref(leaf), "parent", SubjectRef.of("blog/folder", str(role.pk))
            ),
        ]
    )
    with no_enumeration(active):
        assert set(Folder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {
            member.pk,
            role.pk,
            leaf.pk,
        }
        assert not Folder.objects.with_actor(OUTSIDER).exists()
        assert active.check_access(
            subject=ACTOR, action="read", resource=to_object_ref(leaf)
        ).allowed
        predicate = LocalQueryScope(active, ACTOR, "default").predicate(
            Folder, "read", "blog/folder"
        )
        assert isinstance(predicate.children[0], DepthCheckedPredicate)
        # Flat paths repeat shared prefixes to keep parser depth constant.
        # Composed recursion remains bounded; the guard above still refuses
        # multiple self-arrows before exponential expansion.
        assert len(str(Folder._base_manager.filter(predicate).query)) < 500_000


def test_fresh_compiler_has_immutable_frames_and_captures_limit(active):
    active.set_schema(parse_zed(COMPOSED))
    with override_settings(REBAC_DEPTH_LIMIT=1):
        scope = RecursiveQueryScope(active, ACTOR, "default")
    with override_settings(REBAC_DEPTH_LIMIT=3):
        assert scope.depth_limit == 1
        args = ("blog/folder", "effective_member", Folder, "pk", frozenset())
        first = scope.permission(*args)
        scope.hop(*args)
        scope.permission(*args, _CompileContext(boundary=True))
        last = scope.permission(*args)
        assert (
            Folder._base_manager.filter(first).query.sql_with_params()
            == Folder._base_manager.filter(last).query.sql_with_params()
        )
    context = _CompileContext(depth=1, boundary=True)
    with pytest.raises(FrozenInstanceError):
        context.depth = 2


def test_missing_subject_set_definition_denies_its_branch_without_fallback(active):
    active.set_schema(
        parse_zed("""
        definition auth/user {}
        definition blog/folder {
            relation owner: auth/user
            relation absent: auth/group#member
            relation parent: blog/folder
            permission read = (owner + absent) + parent->read
            permission missing = absent
            permission exclusion = authenticated - absent
        }
    """)
    )
    with sudo(reason="missing definition regression"):
        row = Folder.objects.create(name="missing group")
    active.write_relationships(
        [
            RelationshipTuple(
                to_object_ref(row), "absent", SubjectRef.of("auth/group", "missing", "member")
            ),
        ]
    )
    with no_enumeration(active):
        for action, allowed in (("read", False), ("missing", False), ("exclusion", True)):
            assert Folder.objects.with_actor(ACTOR).with_action(action).exists() is allowed
            assert (
                active.check_access(
                    subject=ACTOR, action=action, resource=to_object_ref(row)
                ).allowed
                is allowed
            )
        grant(active, row, "owner")
        assert Folder.objects.with_actor(ACTOR).exists()


def test_testapp_schema_sync_compiles_recursive_permissions(active):
    call_command("rebac", "sync", stdout=StringIO())
    call_command("rebac", "sync", "--check", stdout=StringIO())
    # Read the persisted schema, rather than hiding a missing source definition
    # with an independently authored schema fixture.
    reset_backend()
    active = backend()
    source = Path(__file__).parent / "testapp/permissions.zed"
    assert parse_zed(source.read_text()).get_definition("auth/group") is not None
    with no_enumeration(active):
        for model, action in ((Folder, "read"), (Folder, "write"), (Post, "read")):
            predicate = LocalQueryScope(active, ACTOR, "default").predicate(
                model, action, model._meta.rebac_resource_type
            )
            assert isinstance(predicate.children[0], DepthCheckedPredicate)
            model._base_manager.filter(predicate).query.sql_with_params()
