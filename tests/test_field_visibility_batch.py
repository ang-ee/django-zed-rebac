"""Autocommit evaluator coverage for batched field-read decisions."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, models
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import (
    ObjectRef,
    PermissionEvaluator,
    RelationshipTuple,
    SubjectRef,
    backend,
    evaluator_scope,
    sudo,
)
from rebac.backends import reset_backend
from rebac.testing import install_schema
from tests.backend_setup import atomic_source_write
from tests.testapp.models import Post


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_field_visibility_evaluator_memo_skips_second_materialization():
    reset_backend()
    active = backend()
    install_schema(
        """
        definition auth/user {}
        definition blog/post {
            relation owner: auth/user
            relation viewer: auth/user
            permission read = owner + viewer
            permission read_private = owner
            permission read__title = read_private
            permission read__body = read_private
        }
        """,
        backend=active,
    )
    alice = atomic_source_write(get_user_model().objects.create, username="alice")
    actor = SubjectRef.of("auth/user", str(alice.pk))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="private", body="private")
    resource = ObjectRef("blog/post", str(post.pk))
    active.write_relationships([RelationshipTuple(resource, "viewer", actor)])
    with evaluator_scope() as evaluator:
        with CaptureQueriesContext(connection) as first:
            first_rows = list(Post.objects.with_actor(actor))
        with CaptureQueriesContext(connection) as second:
            second_rows = list(Post.objects.with_actor(actor))
        assert len(first) == 2  # One row statement and one shared visibility statement.
        assert len(second) == 1  # The row statement only, zero visibility statements.
        assert evaluator.stats()["visibility_entries"] == 2
        owner = RelationshipTuple(resource, "owner", actor)
        active.write_relationships([owner])
        with CaptureQueriesContext(connection) as after_grant:
            granted_rows = list(Post.objects.with_actor(actor))
        assert len(after_grant) == 2  # The relationship generation invalidated the memo.
        active.delete_relationship(owner)
        with CaptureQueriesContext(connection) as after_revoke:
            revoked_rows = list(Post.objects.with_actor(actor))
        assert len(after_revoke) == 2
    assert first_rows[0].title is None and first_rows[0].body is None
    assert second_rows[0].title is None and second_rows[0].body is None
    assert granted_rows[0].title == "private" and granted_rows[0].body == "private"
    assert revoked_rows[0].title is None and revoked_rows[0].body is None


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_live_backed_field_rechecks_after_plain_queryset_update():
    from tests.testapp.models import AuthoredPost

    reset_backend()
    active = backend()
    install_schema(
        """
        definition auth/user {}
        definition blog/authoredpost {
            relation owner: auth/user // rebac:field=author
            relation viewer: auth/user
            permission read = owner + viewer
            permission read__title = owner
        }
        """,
        backend=active,
    )
    alice = atomic_source_write(get_user_model().objects.create, username="alice")
    bob = atomic_source_write(get_user_model().objects.create, username="bob")
    actor = SubjectRef.of("auth/user", str(alice.pk))
    with sudo(reason="test.fixture"):
        post = AuthoredPost.objects.create(title="secret", author=alice)
    active.write_relationships(
        [RelationshipTuple(ObjectRef("blog/authoredpost", str(post.pk)), "viewer", actor)]
    )
    with evaluator_scope() as evaluator:
        assert [row.title for row in AuthoredPost.objects.with_actor(actor)] == ["secret"]
        models.QuerySet(AuthoredPost).filter(pk=post.pk).update(author=bob)
        with CaptureQueriesContext(connection) as second:
            assert [row.title for row in AuthoredPost.objects.with_actor(actor)] == [None]
        assert len(second) == 2  # Row plus a fresh field decision after the column change.
        assert evaluator.stats()["visibility_entries"] == 0


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
def test_visibility_memo_budget_counts_candidate_ids():
    from rebac.field_visibility import visible_id_sets

    reset_backend()
    active = backend()
    install_schema(
        """
        definition auth/user {}
        definition blog/post {
            relation owner: auth/user
            permission read = owner
            permission read__title = owner
        }
        """,
        backend=active,
    )
    with sudo(reason="test.fixture"):
        posts = [Post.objects.create(title=f"post {index}") for index in range(4)]
    ids = tuple(str(post.pk) for post in posts)
    actor = SubjectRef.of("auth/user", "budget")
    arguments = dict(model=Post, actor=actor, fields=frozenset({"title"}))
    with evaluator_scope(PermissionEvaluator(max_size=3)) as evaluator:
        visible_id_sets(**arguments, resource_ids=ids[:2])
        assert evaluator.stats()["visibility_ids"] == 2
        visible_id_sets(**arguments, resource_ids=ids[2:])
        assert evaluator.stats()["visibility_entries"] == 1
        assert evaluator.stats()["visibility_ids"] == 2
        with CaptureQueriesContext(connection) as evicted:
            visible_id_sets(**arguments, resource_ids=ids[:2])
        assert len(evicted) == 1
        evaluator.invalidate()
        assert evaluator.stats()["visibility_ids"] == 0


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
def test_held_many_chunks_after_500_ids_with_linear_sql_bytes():
    reset_backend()
    active = backend()
    install_schema(
        """
        definition auth/user {}
        definition blog/post {
            relation owner: auth/user
            permission read = owner
            permission read__title = owner
            permission read__body = owner
        }
        """,
        backend=active,
    )
    with sudo(reason="test.fixture"):
        Post._base_manager.bulk_create([Post(title=f"post {index}") for index in range(501)])
    ids = tuple(str(pk) for pk in Post._base_manager.order_by("pk").values_list("pk", flat=True))
    actor = SubjectRef.of("auth/user", "byte-budget")
    active.write_relationships([RelationshipTuple(ObjectRef("blog/post", ids[0]), "owner", actor)])
    arguments = dict(
        subject=actor,
        actions=("read__title", "read__body"),
        resource_type="blog/post",
    )
    with CaptureQueriesContext(connection) as first:
        at_500 = active.held_many(**arguments, ids=ids[:500])
    with CaptureQueriesContext(connection) as second:
        at_501 = active.held_many(**arguments, ids=ids)
    first_bytes = sum(len(query["sql"].encode()) for query in first)
    second_bytes = sum(len(query["sql"].encode()) for query in second)
    assert len(first) == 1 and len(second) == 2
    assert at_500 == {"read__title": {ids[0]}, "read__body": {ids[0]}}
    assert at_501 == at_500
    assert first_bytes < 10_000
    # Bytes grow by statement, not by id: every statement carries its permission
    # predicate inline, so the remainder is one more bounded statement.
    assert max(len(query["sql"].encode()) for query in second) < 10_000
    assert second_bytes < 2 * first_bytes
    with pytest.raises(TypeError, match="bare string"):
        active.held_many(subject=actor, actions="read__title", resource_type="blog/post", ids=ids)
