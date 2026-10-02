"""Recursion shapes and three-state results, checked through the public reads."""

import pytest
from django.db.models import F, Value

from rebac import ObjectRef, PermissionDepthExceeded, RelationshipTuple, SubjectRef, sudo
from rebac.compile import At, Bound, Compiler
from rebac.compile.read import check
from rebac.models.generation import SchemaGeneration
from rebac.testing import install_schema
from rebac.types import CheckResult
from tests.testapp.models import Folder

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("viewer - viewer", CheckResult.conditional(("flag",))),
        ("viewer + (authenticated - viewer)", CheckResult.conditional(("flag",))),
        ("viewer + authenticated", CheckResult.has()),
    ],
)
def test_conditional_set_operations_preserve_three_state_contract(expression, expected):
    local = install_schema(f"""
        caveat guarded(flag bool) {{ flag }}
        definition auth/user {{}}
        definition blog/post {{
            relation viewer: auth/user with guarded
            permission read = {expression}
        }}
    """)
    actor = SubjectRef.of("auth/user", "alice")
    resource = ObjectRef("blog/post", "one")
    local.write_relationships([RelationshipTuple(resource, "viewer", actor, caveat_name="guarded")])
    assert local.check_access(subject=actor, action="read", resource=resource) == expected
    assert (
        check(
            backend=local,
            resource=resource,
            action="read",
            actor=actor,
            context=None,
            using="default",
        )
        == expected
    )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("flag", [None, False, True])
def test_alias_cycle_retains_caveat_bounds_under_exclusion(settings, storage, flag):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    settings.REBAC_DEPTH_LIMIT = 1
    local = install_schema("""
        caveat guarded(flag bool) { flag }
        definition auth/user {}
        definition blog/post {
            relation viewer: auth/user with guarded
            permission read = alias
            permission alias = read + viewer
            permission outside = authenticated - read
        }
    """)
    actor = SubjectRef.of("auth/user", "alice")
    resource = ObjectRef("blog/post", "one")
    local.write_relationships([RelationshipTuple(resource, "viewer", actor, caveat_name="guarded")])
    context = {} if flag is None else {"flag": flag}
    for action, allowed in (("read", flag), ("outside", None if flag is None else not flag)):
        expected = (
            CheckResult.conditional(("flag",))
            if allowed is None
            else CheckResult.has()
            if allowed
            else CheckResult.no()
        )
        assert (
            check(
                backend=local,
                resource=resource,
                action=action,
                actor=actor,
                context=context,
                using="default",
            )
            == expected
        )


@pytest.mark.parametrize("hops", [2, 12])
def test_structural_folder_recursion_uses_sound_lower_and_upper_bounds(hops):
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation owner: auth/user
            relation parent: blog/folder // rebac:field=parent
            permission read = owner + parent->read
            permission outside = authenticated - read
        }
    """)
    actor = SubjectRef.of("auth/user", "alice")
    folders = [
        Folder(
            pk=1001 + offset, name=f"depth-{offset}", parent_id=1000 + offset if offset else None
        )
        for offset in range(hops + 1)
    ]
    with sudo(reason="recursive compiler fixtures"):
        Folder._base_manager.bulk_create(folders)
    local.write_relationships([RelationshipTuple(ObjectRef("blog/folder", "1001"), "owner", actor)])
    deepest = str(folders[-1].pk)
    point = At("blog/folder", Value(deepest), None, False)
    scope = At("blog/folder", F("pk"), Folder._meta.pk, True)

    def point_holds(compiler, action, bound):
        return (
            SchemaGeneration.objects.filter(pk=1)
            .filter(compiler.holds(("blog/folder", action), point, bound))
            .exists()
        )

    limited = Compiler(local.schema(), actor, "default", depth_limit=1 if hops == 2 else 8)
    assert not point_holds(limited, "read", Bound.LOWER)
    assert point_holds(limited, "read", Bound.UPPER)
    # The unresolved inherited grant cannot become a definite allow under exclusion.
    assert not point_holds(limited, "outside", Bound.LOWER)
    assert point_holds(limited, "outside", Bound.UPPER)
    assert (
        SchemaGeneration.objects.filter(pk=1)
        .filter(limited.depth_unknown(("blog/folder", "outside"), point))
        .exists()
    )

    complete = Compiler(local.schema(), actor, "default", depth_limit=4 if hops == 2 else 16)
    assert point_holds(complete, "read", Bound.LOWER)
    assert not point_holds(complete, "outside", Bound.UPPER)
    assert (
        Folder._base_manager.filter(pk=folders[-1].pk)
        .filter(complete.holds(("blog/folder", "read"), scope, Bound.LOWER))
        .exists()
    )


@pytest.mark.xfail(
    strict=True,
    raises=PermissionDepthExceeded,
    reason="proposal 0015 follow-up: permissions that recurse through each other are unrolled "
    "to the depth bound without a closure, so a data cycle through them does not converge",
)
def test_denied_check_through_a_cycle_of_mutually_recursive_permissions_is_decided():
    local = install_schema("""
        definition auth/user {}
        definition test/folder {
            relation viewer: auth/user
            relation project: test/project
            permission view = viewer + project->access
        }
        definition test/project {
            relation member: auth/user
            relation folder: test/folder
            permission access = member + folder->view
        }
    """)
    folder, project = ObjectRef("test/folder", "f"), ObjectRef("test/project", "p")
    local.write_relationships(
        [
            RelationshipTuple(folder, "project", SubjectRef.of("test/project", "p")),
            RelationshipTuple(project, "folder", SubjectRef.of("test/folder", "f")),
        ]
    )
    alice = SubjectRef.of("auth/user", "alice")
    assert not local.check_access(subject=alice, action="view", resource=folder).allowed
    local.write_relationships([RelationshipTuple(project, "member", alice)])
    assert local.check_access(subject=alice, action="view", resource=folder).allowed


def test_arrows_to_a_like_named_permission_of_another_type_keep_the_self_foreign_key_form(
    settings,
):
    local = install_schema("""
        definition auth/user {}
        definition docs/drive {
            relation viewer: auth/user
            permission read = viewer
        }
        definition blog/folder {
            relation drive: docs/drive
            relation parent: blog/folder // rebac:field=parent
            relation viewer: auth/user
            permission read = ((drive->read + parent->read) + viewer)
        }
    """)
    actor = SubjectRef.of("auth/user", "alice")
    with sudo(reason="test.fixture"):
        root = Folder.objects.create(name="root")
        middle = Folder.objects.create(name="middle", parent=root)
        leaf = Folder.objects.create(name="leaf", parent=middle)
        other = Folder.objects.create(name="other")
    local.write_relationships(
        [
            RelationshipTuple(
                ObjectRef("blog/folder", str(middle.pk)), "drive", SubjectRef.of("docs/drive", "d")
            ),
            RelationshipTuple(ObjectRef("docs/drive", "d"), "viewer", actor),
        ]
    )
    sizes = []
    for limit in (4, 12):
        settings.REBAC_DEPTH_LIMIT = limit
        scoped = Folder.objects.with_actor(actor).scoped()
        assert set(scoped.values_list("pk", flat=True)) == {middle.pk, leaf.pk}
        sql, _params = scoped.query.sql_with_params()
        # The base is named once whatever the bound: only the chain grows.
        sizes.append((sql.count("docs/drive"), len(sql)))
    assert sizes[0][0] == sizes[1][0]
    assert sizes[1][1] - sizes[0][1] < 2000
    assert not local.check_access(
        subject=actor, action="read", resource=ObjectRef("blog/folder", str(other.pk))
    ).allowed
    assert root.pk not in set(
        Folder.objects.with_actor(actor).scoped().values_list("pk", flat=True)
    )
