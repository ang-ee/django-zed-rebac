"""Decision caches must respect tuple expiry, transactions, and backend writes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from rebac import LocalBackend, ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope
from rebac.models import SchemaDefinition, SchemaPermission, SchemaRelation
from rebac.schema import parse_zed
from rebac.types import RelationshipFilter

ACTOR = SubjectRef.of("auth/user", "alice")
RESOURCE = ObjectRef("blog/post", "1")

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["denormalized", "registry"])
def storage(request, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param


def _backend(*, persisted=False, expiration=False):
    local = LocalBackend()
    if persisted:
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="blog/post")
        SchemaRelation.objects.create(
            definition=definition,
            name="viewer",
            allowed_subjects=[{"type": "auth/user"}],
            with_expiration=expiration,
        )
        SchemaPermission.objects.create(definition=definition, name="read", expression="viewer")
    else:
        modifier = " with expiration" if expiration else ""
        local.set_schema(
            parse_zed(
                "use expiration\ndefinition auth/user {}\n"
                f"definition blog/post {{ relation viewer: auth/user{modifier} "
                "permission read = viewer }"
            )
        )
    return local


def _allowed(evaluator, local, surface):
    if surface == "check":
        return evaluator.check(local, subject=ACTOR, action="read", resource=RESOURCE).allowed
    return "1" in evaluator.accessible(
        local, subject=ACTOR, action="read", resource_type="blog/post"
    )


@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("surface", ["check", "accessible"])
def test_cached_relationship_grant_ends_at_expiration(storage, persisted, surface, monkeypatch):
    local = _backend(persisted=persisted, expiration=True)
    deadline = timezone.now() + timedelta(seconds=1)
    local.write_relationships([RelationshipTuple(RESOURCE, "viewer", ACTOR, expires_at=deadline)])
    with evaluator_scope() as evaluator:
        assert _allowed(evaluator, local, surface)
        monkeypatch.setattr(timezone, "now", lambda: deadline)
        assert not _allowed(evaluator, local, surface)


@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("surface", ["check", "accessible"])
def test_rolled_back_relationship_cannot_remain_cached(storage, persisted, surface):
    local = _backend(persisted=persisted)
    with evaluator_scope() as evaluator:
        with transaction.atomic():
            local.write_relationships([RelationshipTuple(RESOURCE, "viewer", ACTOR)])
            assert _allowed(evaluator, local, surface)
            transaction.set_rollback(True)
        assert not _allowed(evaluator, local, surface)


@pytest.mark.parametrize("surface", ["check", "accessible"])
@pytest.mark.parametrize("mutation", ["write", "delete", "delete_exact"])
def test_backend_mutation_invalidates_suspended_evaluator(storage, surface, mutation):
    local = _backend()
    writer = _backend()
    row = RelationshipTuple(RESOURCE, "viewer", ACTOR)
    if mutation != "write":
        writer.write_relationships([row])
    with evaluator_scope() as evaluator:
        assert _allowed(evaluator, local, surface) is (mutation != "write")
        with evaluator_scope():
            if mutation == "write":
                writer.write_relationships([row])
            elif mutation == "delete":
                writer.delete_relationships(RelationshipFilter(resource_type="blog/post"))
            else:
                writer.delete_relationship(row)
        assert _allowed(evaluator, local, surface) is (mutation == "write")
