"""Inherited constants choose scope plans without granting from cached facts."""

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope
from rebac.models import active_relationship_model
from rebac.testing import install_schema
from tests.test_scope_plans import ADMIN, MEMBER, SCHEMA, _policy, _small_rows
from tests.testapp.models import Folder, Post, ScopePart

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pg_delta]


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("chain", [False, True])
def test_inherited_witness_rechecks_revocation(storage, chain, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    _policy()
    if chain:
        install_schema(
            SCHEMA.replace(
                "permission read = ((admin->member + direct) + group) + thread->read",
                "permission read = thread->read",
            )
        )
    _small_rows()
    with evaluator_scope():
        with CaptureQueriesContext(connection) as first:
            prepared = ScopePart.objects.with_actor(ADMIN).scoped()
        assert len(first) == 1  # One distinct witness; the policy is already warm.
        assert list(prepared.values_list("fragment_id", flat=True)) == [1, 2, 3]
        with CaptureQueriesContext(connection) as again:
            ScopePart.objects.with_actor(ADMIN).scoped()
        assert len(again) == 0
        # Another writer bypasses the library's generation invalidation.
        active_relationship_model()._base_manager.filter(relation="direct").delete()
        assert list(prepared.values_list("fragment_id", flat=True)) == []
        assert list(ScopePart.objects.with_actor(ADMIN).values_list("fragment_id", flat=True)) == []


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize(
    "expression", ["folder->read", "folder->read + own", "folder->read & own", "own - folder->read"]
)
def test_inherited_witness_requires_live_target_and_preserves_other_arms(
    storage, expression, settings
):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(
        """
        definition auth/user {}
        definition scope/role { relation member: auth/user }
        definition blog/folder {
            relation admin: scope/role // rebac:const=admin
            relation viewer: auth/user
            permission read = admin->member + viewer
        }
        definition blog/post {
            relation folder: blog/folder // rebac:field=folder
            relation own: auth/user
            permission read = """
        + expression
        + """
        }
    """
    )
    [folder] = Folder._base_manager.bulk_create([Folder(name="live")])
    linked, orphan = Post._base_manager.bulk_create(
        [Post(title="linked", folder=folder), Post(title="no target")]
    )
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("scope/role", "admin"), "member", ADMIN),
            RelationshipTuple(ObjectRef("blog/post", str(orphan.pk)), "own", ADMIN),
            RelationshipTuple(ObjectRef("blog/folder", str(folder.pk)), "viewer", MEMBER),
        ]
    )
    for actor in (ADMIN, MEMBER):
        expected = list(Post.objects.with_actor(actor).order_by("pk").values_list("pk", flat=True))
        with evaluator_scope():
            assert (
                list(Post.objects.with_actor(actor).order_by("pk").values_list("pk", flat=True))
                == expected
            )
    assert (
        linked.pk in list(Post.objects.with_actor(ADMIN).values_list("pk", flat=True))
        or "&" in expression
        or "-" in expression
    )


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_own_and_distinct_inherited_witnesses_share_one_statement(storage, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(
        """
        definition auth/user {}
        definition scope/role { relation member: auth/user }
        definition blog/folder {
            relation admin: scope/role // rebac:const=parent_admin
            relation viewer: auth/user
            permission read = admin->member + viewer
        }
        definition blog/post {
            relation admin: scope/role // rebac:const=child_admin
            relation folder: blog/folder // rebac:field=folder
            permission read = admin->member + folder->read
        }
        """
    )
    [folder] = Folder._base_manager.bulk_create([Folder(name="live")])
    [post] = Post._base_manager.bulk_create([Post(title="linked", folder=folder)])
    local.write_relationships(
        [RelationshipTuple(ObjectRef("scope/role", "parent_admin"), "member", ADMIN)]
    )
    with evaluator_scope() as evaluator:
        with CaptureQueriesContext(connection) as first:
            prepared = Post.objects.with_actor(ADMIN).scoped()
        assert len(first) == 1
        assert evaluator.stats()["constant_witness_entries"] == 2
        assert list(prepared.values_list("pk", flat=True)) == [post.pk]
        with CaptureQueriesContext(connection) as repeat:
            Post.objects.with_actor(ADMIN).scoped()
            Folder.objects.with_actor(ADMIN).scoped()
        assert len(repeat) == 0


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("atomic", [False, True])
def test_negative_inherited_witness_keeps_live_constant_arms(storage, atomic, settings):
    from contextlib import nullcontext

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = _policy()
    _small_rows()
    newcomer = SubjectRef.of("auth/user", "newcomer")
    with transaction.atomic() if atomic else nullcontext(), evaluator_scope():
        prepared = ScopePart.objects.with_actor(newcomer).scoped()
        assert not prepared.exists()
        local.write_relationships(
            [RelationshipTuple(ObjectRef("scope/role", "staff"), "direct", newcomer)]
        )
        assert list(prepared.values_list("fragment_id", flat=True)) == [1, 2, 3]
