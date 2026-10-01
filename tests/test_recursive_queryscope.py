"""Index scopes preserve recursive decisions without a read-depth bound."""

from __future__ import annotations

from contextlib import contextmanager
from itertools import pairwise
from unittest.mock import patch

import pytest
from django.db import connection
from django.db.models import Count, Exists, OuterRef
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import (
    PermissionDepthExceeded,
    RelationshipTuple,
    SubjectRef,
    app_settings,
    backend,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import STORAGE_TIERS, install_schema
from tests.testapp.models import Folder, Post

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
        install_schema(active, parse_zed(text))
        try:
            yield active, member, hop, action
        finally:
            reset_backend()


@contextmanager
def no_enumeration(active):
    with patch.object(active, "accessible", side_effect=AssertionError("enumeration fallback")):
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


@contextmanager
def beyond_depth_limit(depth):
    """Keep a chain of ``depth`` hops past REBAC_DEPTH_LIMIT, the bound recursion is unrolled to.

    A short tier-1 chain lowers the limit below its depth; a long chain keeps the default.
    """
    with override_settings(REBAC_DEPTH_LIMIT=min(app_settings.REBAC_DEPTH_LIMIT, depth - 1)):
        yield


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("shape", ["role", "folder"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
@pytest.mark.parametrize("depth", [0, 1, 2, pytest.param(8, marks=pytest.mark.slow)])
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
@pytest.mark.parametrize("depth", [3, pytest.param(12, marks=pytest.mark.slow)])
def test_recursion_is_decided_within_the_depth_limit_and_refused_beyond_it(
    storage, shape, backing, depth
):
    with (
        beyond_depth_limit(depth),
        schema_context(storage, shape, backing) as (active, member, hop, action),
    ):
        limit = app_settings.REBAC_DEPTH_LIMIT
        rows = chain(active, hop, backing, depth)
        grant(active, rows[0], member)
        scoped = Folder.objects.with_actor(ACTOR).with_action(action)
        for row in (rows[0], rows[limit]):
            assert active.check_access(
                subject=ACTOR, action=action, resource=to_object_ref(row)
            ).allowed
            assert scoped.filter(pk=row.pk).exists()
        # Past the bound a point check is refused, never answered by truncation,
        # and a scope leaves the row out.
        with pytest.raises(PermissionDepthExceeded):
            active.check_access(subject=ACTOR, action=action, resource=to_object_ref(rows[-1]))
        assert not scoped.filter(pk=rows[-1].pk).exists()
        for actor in (OUTSIDER, ANONYMOUS):
            assert not active.check_access(
                subject=actor, action=action, resource=to_object_ref(rows[0])
            ).allowed
            assert (
                not Folder.objects.with_actor(actor)
                .with_action(action)
                .filter(pk__in=[rows[0].pk, rows[-1].pk])
                .exists()
            )
        with override_settings(REBAC_DEPTH_LIMIT=depth):
            assert active.check_access(
                subject=ACTOR, action=action, resource=to_object_ref(rows[-1])
            ).allowed
            assert scoped.filter(pk=rows[-1].pk).exists()


@pytest.mark.parametrize("storage", STORAGE_TIERS)
@pytest.mark.parametrize("shape", ["role", "folder"])
@pytest.mark.parametrize("backing", ["tuple", "field", "path"])
def test_recursive_builtin_and_exclusion(storage, shape, backing):
    with schema_context(storage, shape, backing, builtin=True) as (active, _member, hop, action):
        rows = chain(active, hop, backing, 2)
        Folder.objects.sudo(reason="backing fixture update").filter(pk=rows[0].pk).update(
            is_active=True
        )
        if backing == "path":
            Folder.objects.sudo(reason="backing fixture update").filter(pk=rows[1].pk).update(
                is_active=True
            )
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


@pytest.mark.parametrize("storage", STORAGE_TIERS)
@pytest.mark.parametrize("backing", ["tuple", "field"])
@pytest.mark.parametrize("depth", [3, pytest.param(8, marks=pytest.mark.slow)])
def test_early_grant_and_live_boundary(storage, backing, depth):
    with schema_context(storage, "folder", backing) as (active, member, hop, _action):
        rows = chain(active, hop, backing, depth)
        grant(active, rows[0], member)
        pending = Folder.objects.with_actor(ACTOR).scoped().filter(pk=rows[-1].pk)
        with no_enumeration(active):
            assert pending.exists()
            if backing == "field":
                Folder.objects.sudo(reason="backing fixture update").filter(pk=rows[0].pk).update(
                    parent=rows[-1]
                )
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
            assert not pending.exists()


@pytest.mark.pg_delta
@pytest.mark.parametrize("storage", STORAGE_TIERS)
@pytest.mark.parametrize("backing", ["tuple", "field"])
@pytest.mark.parametrize("deep", [3, pytest.param(8, marks=pytest.mark.slow)])
def test_scope_query_count_is_independent_of_depth(storage, backing, deep):
    costs = []
    with schema_context(storage, "folder", backing) as (active, member, hop, action):
        for depth in (1, deep):
            rows = chain(active, hop, backing, depth, prefix=str(depth))
            grant(active, rows[0], member)
            qs = (
                Folder.objects.with_actor(ACTOR).with_action(action).filter(pk=rows[-1].pk).scoped()
            )
            with CaptureQueriesContext(connection) as queries:
                assert qs.exists()
            costs.append(len(queries))
    # The actor's stored sets, then the scope.
    assert costs == [2, 2]


@pytest.mark.pg_delta
@pytest.mark.parametrize("storage", STORAGE_TIERS)
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
            assert (
                Folder.objects.sudo(reason="backing fixture update")
                .filter(pk__in=scoped.values("pk"))
                .count()
                == 3
            )
            related = scoped.filter(pk=OuterRef("folder_id"))
            assert list(
                Post._base_manager.filter(Exists(related)).values_list("pk", flat=True)
            ) == [post.pk]
            assert scoped.with_actor(OUTSIDER).count() == 0
            assert (scoped.filter(pk=rows[0].pk) | scoped.filter(pk=rows[1].pk)).count() == 2
            assert scoped.filter(pk=rows[0].pk).union(scoped.filter(pk=rows[1].pk)).count() == 2


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
def test_recursive_group_before_self_arrow_still_compiles(storage, backing):
    with (
        schema_context(storage, "role", backing) as (active, member, hop, action),
    ):
        text, *_ = recursive_schema("role", backing)
        install_schema(
            active,
            parse_zed(
                text.replace(
                    "relation member: auth/user }",
                    "relation member: auth/user | auth/group#member }",
                )
            ),
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


def test_set_operation_cycle_is_refused():
    from rebac.compile.program import program_errors

    text, *_ = recursive_schema("folder", "tuple")
    schema = parse_zed(
        text.replace("(reader) + parent->read", "(authenticated - alias) + parent->read")
    )
    errors = program_errors(schema)
    assert errors
    assert {error.id for error in errors} == {"rebac.E016"}


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_missing_subject_set_definition_denies_its_branch_without_fallback(storage):
    from rebac import backend

    with schema_context(storage, "folder", "tuple"):
        _missing_subject_set_definition(backend())


def _missing_subject_set_definition(active):
    install_schema(
        active,
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
    """),
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
