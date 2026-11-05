"""The owned multi-action closure projects several verdicts from one proof."""

import re

import pytest
from django.db import connection
from django.db.models import Exists, OuterRef
from django.test.utils import CaptureQueriesContext

from rebac import (
    SCOPE_CTE_CANDIDATE_LIMIT,
    RelationshipTuple,
    SubjectRef,
    sudo,
    to_object_ref,
)
from rebac.expressions import permission_expression
from rebac.testing import install_schema
from tests.testapp.models import Folder

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


@pytest.fixture
def rows():
    local = install_schema("""
        definition auth/user {}
        definition blog/folder {
            relation owner: auth/user
            relation viewer: auth/user
            relation banned: auth/user
            permission read = owner + viewer
            permission write = owner
            permission share = (owner + viewer) - banned
            permission delete = owner - banned
        }
    """)
    actor = SubjectRef.of("auth/user", "multi-action")
    with sudo(reason="multi-action fixture"):
        folders = [Folder.objects.create(name=f"multi-{n}") for n in range(4)]
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(folders[0]), "owner", actor),
            RelationshipTuple(to_object_ref(folders[1]), "viewer", actor),
            RelationshipTuple(to_object_ref(folders[1]), "banned", actor),
        ]
    )
    return actor, folders


def _projections(actor, folders, actions):
    return (
        Folder._base_manager.filter(pk__in=[row.pk for row in folders])
        .order_by("pk")
        .annotate(
            **{
                f"can_{action}": permission_expression(Folder, action, actor=actor)
                for action in actions
            }
        )
    )


@pytest.mark.parametrize("bounded", [False, True])
def test_four_actions_share_one_bounded_closure(rows, bounded):
    actor, folders = rows
    actions = ("read", "write", "share", "delete")
    source = Folder.objects.with_actor(actor)
    if bounded:
        source = source.filter(pk__in=[row.pk for row in folders])
    handle = source.scope_cte(actions=actions)
    outer = _projections(actor, folders, actions)
    attached = handle.attach(outer)
    fields = ("pk", *(f"can_{action}" for action in actions))
    assert len(re.findall(r'"rebac_scope_[^"]+_closure" AS \(', str(attached.query))) == 1
    with CaptureQueriesContext(connection) as queries:
        assert list(attached.values_list(*fields)) == list(outer.values_list(*fields))
    assert len(queries) == 2
    assert list(attached.values_list(*fields)) == [
        (folders[0].pk, True, True, True, True),
        (folders[1].pk, True, False, False, False),
        (folders[2].pk, False, False, False, False),
        (folders[3].pk, False, False, False, False),
    ]
    assert (
        len(re.findall(r'"rebac_scope_[^"]+_closure" AS \(', str(handle.attach(attached).query)))
        == 1
    )


def test_identity_reference_and_explicit_bounds(rows):
    actor, folders = rows
    handle = (
        Folder.objects.with_actor(actor)
        .filter(pk__in=[row.pk for row in folders[:2]])
        .scope_cte(actions=("read", "write"))
    )
    outer = Folder._base_manager.filter(pk__in=[row.pk for row in folders]).annotate(
        admitted=Exists(handle.queryset().filter(pk=OuterRef("pk")))
    )
    assert list(handle.attach(outer).order_by("pk").values_list("admitted", flat=True)) == [
        True,
        True,
        False,
        False,
    ]
    with pytest.raises(ValueError, match="bounded identity"):
        Folder.objects.with_actor(actor).filter(pk__in=[]).scope_cte()
    with pytest.raises(ValueError, match="bounded identity"):
        Folder.objects.with_actor(actor).filter(
            pk__in=range(1, SCOPE_CTE_CANDIDATE_LIMIT + 2)
        ).scope_cte()
    with pytest.raises(ValueError, match="distinct"):
        Folder.objects.with_actor(actor).scope_cte(actions=("read", "read"))
    with pytest.raises(ValueError, match="unfiltered"):
        Folder.objects.with_actor(actor).filter(name="multi-0").scope_cte()


def test_caveated_action_falls_back_and_rechecks_after_write():
    local = install_schema("""
        caveat flag(enabled bool) { enabled }
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user with flag
            relation banned: auth/user
            permission read = viewer
            permission write = viewer - banned
        }
    """)
    actor = SubjectRef.of("auth/user", "caveated-scope")
    with sudo(reason="caveated scope fixture"):
        folder = Folder.objects.create(name="caveated-scope")
    local.write_relationships(
        [
            RelationshipTuple(
                to_object_ref(folder),
                "viewer",
                actor,
                caveat_name="flag",
                caveat_context={"enabled": True},
            )
        ]
    )
    handle = (
        Folder.objects.with_actor(actor).filter(pk=folder.pk).scope_cte(actions=("read", "write"))
    )
    outer = Folder._base_manager.annotate(
        can_read=permission_expression(Folder, "read", actor=actor),
        can_write=permission_expression(Folder, "write", actor=actor),
    )
    assert "_closure" not in str(handle.attach(outer).query)
    assert list(handle.attach(outer).values_list("pk", "can_read", "can_write")) == [
        (folder.pk, True, True)
    ]
    local.write_relationships([RelationshipTuple(to_object_ref(folder), "banned", actor)])
    assert list(handle.attach(outer).values_list("pk", "can_read", "can_write")) == [
        (folder.pk, True, False)
    ]
