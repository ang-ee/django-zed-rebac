"""Recursive scopes retain graph decisions and dispatch-depth failures."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import connection
from django.db.models import Count, Exists, OuterRef
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import (
    LocalBackend,
    PermissionDepthExceeded,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from rebac.backends.local_query import LocalQueryScope
from rebac.conf import app_settings
from rebac.schema import parse_zed
from tests.test_queryset_permission_parity import SCHEMA
from tests.testapp.models import AuthoredPost, Folder, Post

pytestmark = pytest.mark.django_db
ACTOR = SubjectRef.of("auth/user", "42")
OUTSIDER = SubjectRef.of("auth/user", "99")
ANONYMOUS = SubjectRef.of("auth/anonymous", "*")
GROUP = SubjectRef.of("auth/group", "editors", "member")


def recursive_schema(shape, backing, *, builtin=False):
    member, hop, action = (
        ("member", "includes", "effective_member")
        if shape == "role"
        else ("reader", "parent", "read")
    )
    annotation = {
        "tuple": "",
        "field": " // rebac:field=parent",
        "path": ' // rebac:field={"path":"parent","filters":{"parent__is_active":true}}',
    }[backing]
    public = " + public->read" if builtin else ""
    text = f"""
    definition auth/user {{}}
    definition auth/group {{ relation member: auth/user }}
    definition site/audience {{ permission read = authenticated }}
    definition blog/folder {{
        relation {member}: auth/user | auth/group#member
        relation {hop}: blog/folder{annotation}
        relation public: site/audience // rebac:const={{"target_id":"all","filters":{{"is_active":true}}}}
        permission {action} = ({member}{public}) + {hop}->{action}
        permission alias = {action}
        permission intersection = authenticated & {action}
        permission exclusion = authenticated - {action}
    }}
    definition blog/post {{
        relation folder: blog/folder // rebac:field=folder
        permission read = folder->{action}
    }}
    """
    return text, member, hop, action


@contextmanager
def schema_context(storage, shape, backing, *, builtin=False):
    text, member, hop, action = recursive_schema(shape, backing, builtin=builtin)
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        reset_backend()
        active = backend()
        active.set_schema(parse_zed(text))
        try:
            yield active, member, hop, action
        finally:
            reset_backend()


@contextmanager
def no_enumeration(active):
    with (
        patch.object(active, "accessible", side_effect=AssertionError("enumeration fallback")),
        patch.object(
            active, "_resources_for_expr", side_effect=AssertionError("grant enumeration")
        ),
        patch.object(
            active, "_resources_via_relation", side_effect=AssertionError("tuple enumeration")
        ),
    ):
        yield


def chain(active, hop, backing, depth, *, prefix="node"):
    with sudo(reason="recursive scope fixture"):
        rows = [Folder.objects.create(name=f"{prefix}-0", is_active=False)]
        for level in range(1, depth + 1):
            rows.append(
                Folder.objects.create(
                    name=f"{prefix}-{level}",
                    parent=rows[-1] if backing != "tuple" else None,
                    is_active=False,
                )
            )
    if backing == "tuple":
        active.write_relationships(
            [
                RelationshipTuple(
                    to_object_ref(child), hop, SubjectRef.of("blog/folder", str(parent.pk))
                )
                for parent, child in pairwise(rows)
            ]
        )
    return rows


def grant(active, row, member, actor=ACTOR):
    active.write_relationships([RelationshipTuple(to_object_ref(row), member, actor)])


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("shape", ["role", "folder"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
@pytest.mark.parametrize("depth", [0, 1, 2, 8])
def test_recursive_chain_parity(storage, shape, backing, depth):
    with schema_context(storage, shape, backing) as (active, member, hop, action):
        rows = chain(active, hop, backing, depth)
        actors = [SubjectRef.of("auth/user", str(100 + i)) for i in range(len(rows))]
        for row, actor in zip(rows, actors, strict=True):
            grant(active, row, member, actor)
        with no_enumeration(active):
            for level, actor in enumerate([*actors, OUTSIDER, ANONYMOUS]):
                expected = {row.pk for row in rows[level:]} if level < len(rows) else set()
                scoped = Folder.objects.with_actor(actor).with_action(action)
                assert set(scoped.values_list("pk", flat=True)) == expected
                for row in rows:
                    assert active.check_access(
                        subject=actor, action=action, resource=to_object_ref(row)
                    ).allowed is (row.pk in expected)
                assert (
                    set(
                        Folder.objects.with_actor(actor)
                        .with_action("alias")
                        .values_list("pk", flat=True)
                    )
                    == expected
                )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("shape", ["role", "folder"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
@pytest.mark.parametrize("actor", [ACTOR, OUTSIDER, ANONYMOUS])
def test_beyond_bound_raises_same_error(storage, shape, backing, actor):
    with schema_context(storage, shape, backing) as (active, member, hop, action):
        rows = chain(active, hop, backing, app_settings.REBAC_DEPTH_LIMIT + 1)
        grant(active, rows[0], member)
        with no_enumeration(active):
            with pytest.raises(PermissionDepthExceeded) as check:
                active.check_access(subject=actor, action=action, resource=to_object_ref(rows[-1]))
            with pytest.raises(PermissionDepthExceeded) as scope:
                Folder.objects.with_actor(actor).with_action(action).filter(pk=rows[-1].pk).count()
            assert str(check.value) == str(scope.value) == "Depth limit 8 exceeded"
            assert Folder.objects.with_actor(actor).with_action(action).filter(
                pk=rows[0].pk
            ).count() == (actor == ACTOR)


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("shape", ["role", "folder"])
@pytest.mark.parametrize("backing", ["tuple", "field", "path"])
def test_recursive_builtin_and_exclusion(storage, shape, backing):
    with schema_context(storage, shape, backing, builtin=True) as (active, _member, hop, action):
        rows = chain(active, hop, backing, 2)
        Folder._base_manager.filter(pk=rows[0].pk).update(is_active=True)
        if backing == "path":
            Folder._base_manager.filter(pk=rows[1].pk).update(is_active=True)
        with sudo(reason="recursive builtin fixture"):
            post = Post.objects.create(title="inherited builtin", folder=rows[-1])
        with no_enumeration(active):
            for actor in (ACTOR, OUTSIDER, ANONYMOUS):
                expected = {r.pk for r in rows} if actor != ANONYMOUS else set()
                for permission in (action, "intersection", "exclusion"):
                    wanted = set() if permission == "exclusion" else expected
                    assert (
                        set(
                            Folder.objects.with_actor(actor)
                            .with_action(permission)
                            .values_list("pk", flat=True)
                        )
                        == wanted
                    )
                    for row in rows:
                        assert active.check_access(
                            subject=actor, action=permission, resource=to_object_ref(row)
                        ).allowed is (row.pk in wanted)
                assert Post.objects.with_actor(actor).filter(pk=post.pk).exists() is (
                    actor != ANONYMOUS
                )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
def test_subject_sets_consume_one_frame(storage, backing):
    with schema_context(storage, "role", backing) as (active, member, hop, action):
        rows = chain(active, hop, backing, app_settings.REBAC_DEPTH_LIMIT)
        grant(active, rows[0], member, GROUP)
        active.write_relationships([RelationshipTuple(GROUP.object, "member", ACTOR)])
        with no_enumeration(active):
            assert (
                Folder.objects.with_actor(ACTOR).with_action(action).filter(pk=rows[-2].pk).exists()
            )
            for evaluate in (
                lambda: active.check_access(
                    subject=ACTOR, action=action, resource=to_object_ref(rows[-1])
                ),
                lambda: (
                    Folder.objects.with_actor(ACTOR)
                    .with_action(action)
                    .filter(pk=rows[-1].pk)
                    .exists()
                ),
            ):
                with pytest.raises(PermissionDepthExceeded, match="Depth limit 8 exceeded"):
                    evaluate()


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
def test_early_grant_and_live_boundary(storage, backing):
    with schema_context(storage, "folder", backing) as (active, member, hop, _action):
        rows = chain(active, hop, backing, app_settings.REBAC_DEPTH_LIMIT)
        grant(active, rows[0], member)
        pending = Folder.objects.with_actor(ACTOR).scoped().filter(pk=rows[-1].pk)
        with no_enumeration(active):
            assert pending.exists()
            if backing == "field":
                Folder._base_manager.filter(pk=rows[0].pk).update(parent=rows[-1])
            else:
                active.write_relationships(
                    [
                        RelationshipTuple(
                            to_object_ref(rows[0]),
                            hop,
                            SubjectRef.of("blog/folder", str(rows[-1].pk)),
                        )
                    ]
                )
            assert pending.exists()
            active.delete_relationship(RelationshipTuple(to_object_ref(rows[0]), member, ACTOR))
            with pytest.raises(PermissionDepthExceeded):
                pending.exists()


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_nonrecursive_sql_is_byte_identical(storage):
    fingerprints = json.loads(
        (Path(__file__).parent / "fixtures/nonrecursive_scope_sql.json").read_text()
    )
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        active = LocalBackend()
        active.set_schema(parse_zed(SCHEMA))
        for model, actions in (
            (Post, ("union_read", "intersection_read", "read", "inherited_read")),
            (AuthoredPost, ("read",)),
        ):
            for action in actions:
                predicate = LocalQueryScope(active, ACTOR, "default").predicate(
                    model, action, model._meta.rebac_resource_type
                )
                query = model._base_manager.filter(predicate).query.sql_with_params()
                assert (
                    hashlib.sha256(repr(query).encode()).hexdigest()
                    == fingerprints[f"{storage}/{model.__name__}/{action}"]
                )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
def test_bounded_scope_has_constant_query_count(storage, backing):
    with schema_context(storage, "folder", backing) as (active, member, hop, _action):
        rows = chain(active, hop, backing, 2)
        grant(active, rows[0], member)
        with no_enumeration(active), CaptureQueriesContext(connection) as queries:
            assert Folder.objects.with_actor(ACTOR).count() == 3
        assert len(queries) == 2


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
@pytest.mark.parametrize("limit", [1, 3])
def test_configured_bound_without_builtin_shortcuts(storage, backing, limit):
    with (
        override_settings(REBAC_DEPTH_LIMIT=limit),
        schema_context(storage, "folder", backing) as (active, member, hop, action),
    ):
        # Remove *all* builtins, including unused definitions. The old direct
        # field-arrow optimization otherwise hid this recursion-depth bug.
        text, *_ = recursive_schema("folder", backing)
        text = text.replace("permission read = authenticated", "permission read = nil")
        text = text.replace("authenticated & read", "read").replace("authenticated - read", "read")
        active.set_schema(parse_zed(text))
        rows = chain(active, hop, backing, limit + 1)
        grant(active, rows[0], member)
        with no_enumeration(active):
            assert active.check_access(
                subject=ACTOR, action=action, resource=to_object_ref(rows[-2])
            ).allowed
            assert Folder.objects.with_actor(ACTOR).filter(pk=rows[-2].pk).exists()
            with pytest.raises(PermissionDepthExceeded, match=f"Depth limit {limit} exceeded"):
                active.check_access(subject=ACTOR, action=action, resource=to_object_ref(rows[-1]))
            with pytest.raises(PermissionDepthExceeded, match=f"Depth limit {limit} exceeded"):
                Folder.objects.with_actor(ACTOR).filter(pk=rows[-1].pk).exists()


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
def test_recursive_scope_composition(storage, backing):
    with schema_context(storage, "folder", backing) as (active, member, hop, _action):
        rows = chain(active, hop, backing, 2)
        grant(active, rows[0], member)
        with sudo(reason="recursive subquery fixture"):
            post = Post.objects.create(title="inherited", folder=rows[-1])
        with no_enumeration(active):
            scoped = Folder.objects.with_actor(ACTOR).scoped()
            assert scoped.aggregate(n=Count("pk")) == {"n": 3}
            assert Folder._base_manager.filter(pk__in=scoped.values("pk")).count() == 3
            related = scoped.filter(pk=OuterRef("folder_id"))
            assert list(
                Post._base_manager.filter(Exists(related)).values_list("pk", flat=True)
            ) == [post.pk]
            assert scoped.with_actor(OUTSIDER).count() == 0
            assert (scoped.filter(pk=rows[0].pk) | scoped.filter(pk=rows[1].pk)).count() == 2
            assert scoped.filter(pk=rows[0].pk).union(scoped.filter(pk=rows[1].pk)).count() == 2


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_frontier_respects_walker_boolean_short_circuit(storage):
    with (
        override_settings(REBAC_DEPTH_LIMIT=1),
        schema_context(storage, "folder", "tuple") as (active, _member, hop, _action),
    ):
        rows = chain(active, hop, "tuple", 2)
        with no_enumeration(active):
            for action in ("intersection", "exclusion"):
                assert not Folder.objects.with_actor(ANONYMOUS).with_action(action).exists()
                assert not active.check_access(
                    subject=ANONYMOUS, action=action, resource=to_object_ref(rows[-1])
                ).allowed
                with pytest.raises(PermissionDepthExceeded):
                    Folder.objects.with_actor(ACTOR).with_action(action).exists()


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
def test_recursive_group_before_self_arrow_still_compiles(storage, backing):
    with (
        override_settings(REBAC_DEPTH_LIMIT=3),
        schema_context(storage, "role", backing) as (active, member, hop, action),
    ):
        text, *_ = recursive_schema("role", backing)
        active.set_schema(
            parse_zed(
                text.replace(
                    "relation member: auth/user }",
                    "relation member: auth/user | auth/group#member }",
                )
            )
        )
        rows = chain(active, hop, backing, 1)
        grant(active, rows[0], member, GROUP)
        nested = SubjectRef.of("auth/group", "nested", "member")
        active.write_relationships(
            [
                RelationshipTuple(GROUP.object, "member", nested),
                RelationshipTuple(nested.object, "member", ACTOR),
            ]
        )
        with no_enumeration(active):
            assert Folder.objects.with_actor(ACTOR).with_action(action).count() == 2
            assert active.check_access(
                subject=ACTOR, action=action, resource=to_object_ref(rows[-1])
            ).allowed


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_alias_cycles_inside_recursive_exclusion_match_walker(storage):
    with (
        override_settings(REBAC_DEPTH_LIMIT=1),
        schema_context(storage, "folder", "tuple") as (active, _member, hop, action),
    ):
        text, *_ = recursive_schema("folder", "tuple")
        active.set_schema(
            parse_zed(
                text.replace("(reader) + parent->read", "(authenticated - alias) + parent->read")
            )
        )
        row = chain(active, hop, "tuple", 0)[0]
        with no_enumeration(active):
            for actor in (ACTOR, ANONYMOUS):
                assert not active.check_access(
                    subject=actor, action=action, resource=to_object_ref(row)
                ).allowed
                assert not Folder.objects.with_actor(actor).exists()
