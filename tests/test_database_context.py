"""Explicit database scopes reuse native policy, tuple and audit owners."""

from __future__ import annotations

import asyncio

import pytest
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import transaction
from django.test import override_settings

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    backend,
    database_context,
    evaluator_scope,
    memberships,
    system_context,
    write_relationships,
)
from rebac.audit import aemit, emit
from rebac.database import read_alias
from rebac.models import (
    PermissionAuditEvent,
    SchemaDefinition,
    SchemaPermission,
    SchemaRelation,
    active_relationship_model,
)
from rebac.models.schema_write import schema_changes
from tests.testapp.models import Post

pytestmark = [
    pytest.mark.django_db(transaction=True, databases=["default", "selected"]),
    pytest.mark.pg_delta,
]


@pytest.fixture(autouse=True, params=["denormalized", "registry"])
def storage(request, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param


ACTOR = SubjectRef.of("auth/user", "1")
RESOURCE = ObjectRef("demo/item", "one")


class DefaultRouter:
    def db_for_read(self, model, **hints):
        return "default"

    def db_for_write(self, model, **hints):
        return "default"


def policy(alias, expression="owner", resource_type="demo/item", write_expression=None):
    with schema_changes(using=alias):
        SchemaDefinition.objects.using(alias).get_or_create(resource_type="auth/user")
        item = SchemaDefinition.objects.using(alias).create(resource_type=resource_type)
        SchemaRelation.objects.using(alias).create(
            definition=item, name="owner", allowed_subjects=[{"type": "auth/user"}]
        )
        SchemaPermission.objects.using(alias).create(
            definition=item, name="read", expression=expression
        )
        if write_expression is not None:
            SchemaPermission.objects.using(alias).create(
                definition=item, name="write", expression=write_expression
            )


def test_scope_keeps_tuples_schema_audit_and_cache_on_one_alias():
    policy("default", "nil")
    policy("selected")
    local = backend()
    with override_settings(DATABASE_ROUTERS=[DefaultRouter()]), evaluator_scope():
        assert not local.check_access(subject=ACTOR, action="read", resource=RESOURCE).allowed
        with database_context("selected"):
            write_relationships([RelationshipTuple(RESOURCE, "owner", ACTOR)])
            assert local.check_access(subject=ACTOR, action="read", resource=RESOURCE).allowed
            assert local._schema_snapshot().using == "selected"
            assert local.schema().get_permission("demo/item", "read").expression != "nil"
            with database_context("default"):
                assert not local.check_access(
                    subject=ACTOR, action="read", resource=RESOURCE
                ).allowed
            assert local.check_access(subject=ACTOR, action="read", resource=RESOURCE).allowed
        assert not local.check_access(subject=ACTOR, action="read", resource=RESOURCE).allowed
    assert active_relationship_model().objects.using("selected").count() == 1
    assert not active_relationship_model().objects.using("default").exists()
    assert PermissionAuditEvent.objects.using("selected").count() == 1
    assert not PermissionAuditEvent.objects.using("default").exists()


def test_deferred_audit_uses_emission_alias_after_scope_exit_and_rolls_back():
    with transaction.atomic(using="selected"):
        with database_context("selected"):
            emit("test", reason="commit")
        assert not PermissionAuditEvent.objects.using("selected").exists()
    assert PermissionAuditEvent.objects.using("selected").get().reason == "commit"
    with pytest.raises(ValueError), transaction.atomic(using="selected"):
        with database_context("selected"):
            emit("test", reason="rollback")
        raise ValueError("rollback")
    assert PermissionAuditEvent.objects.using("selected").count() == 1
    assert not PermissionAuditEvent.objects.using("default").exists()


def test_membership_and_system_audit_follow_scope():
    policy("selected")
    item = SchemaDefinition.objects.using("selected").get(resource_type="demo/item")
    SchemaRelation.objects.using("selected").create(
        definition=item, name="member", allowed_subjects=[{"type": "auth/user"}]
    )
    with override_settings(DATABASE_ROUTERS=[DefaultRouter()]), database_context("selected"):
        row = memberships.grant(subject=ACTOR, container=RESOURCE)
        assert row._state.db == "selected"
        assert list(memberships.members_of(RESOURCE)) == [ACTOR]
        assert list(memberships.containers_of(ACTOR)) == [RESOURCE]
        with system_context(reason="selected operation"):
            pass
        assert memberships.revoke(subject=ACTOR, container=RESOURCE) == 1
        assert list(memberships.members_of(RESOURCE)) == []
    assert not active_relationship_model().objects.using("default").exists()
    assert not PermissionAuditEvent.objects.using("default").exists()
    assert (
        PermissionAuditEvent.objects.using("selected").filter(reason="selected operation").exists()
    )


@pytest.mark.parametrize("exact", [False, True])
def test_explicit_queryset_and_loaded_instance_override_scope(exact):
    policy("default", resource_type="blog/post")
    policy("selected", "nil", resource_type="blog/post")
    with database_context("default"), system_context(reason="fixture"):
        post = Post.objects.using("default").create(title="default row")
        write_relationships(
            [RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "owner", ACTOR)]
        )
    with database_context("selected"), system_context(reason="fixture"):
        Post.objects.using("selected").create(pk=post.pk, title="denied colliding row")
    default = Post.objects.using("default").with_actor(ACTOR)
    selected = Post.objects.using("selected").with_actor(ACTOR)
    if exact:
        default, selected = default.filter(pk=post.pk), selected.filter(pk=post.pk)
    with database_context("selected"):
        assert list(default.values_list("pk", flat=True)) == [post.pk]
        assert post.with_actor(ACTOR).check_access("read").allowed
        assert not selected.exists()
        # A clone changed to the other alias must not retain the first decision.
        assert not default.using("selected").exists()


def test_check_save_denies_on_selected_database_policy():
    policy("default", resource_type="blog/post", write_expression="owner")
    policy("selected", resource_type="blog/post", write_expression="nil")
    with database_context("default"), system_context(reason="fixture"):
        post = Post.objects.using("default").create(title="default row")
        write_relationships(
            [RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "owner", ACTOR)]
        )
    with database_context("selected"), system_context(reason="fixture"):
        Post.objects.using("selected").create(pk=post.pk, title="selected row")
        write_relationships(
            [RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "owner", ACTOR)]
        )
    candidate = Post.objects.using("selected").with_actor(ACTOR).get(pk=post.pk)
    candidate.title = "refused"
    assert not candidate.check_save(using="selected").allowed
    assert not candidate._rebac_gates_admit()
    with pytest.raises(PermissionDenied):
        candidate.save(using="selected")
    assert Post._base_manager.using("selected").get(pk=post.pk).title == "selected row"


def test_nested_failure_and_async_tasks_restore_context():
    async def operation(alias):
        with database_context(alias):
            await asyncio.sleep(0)
            await aemit("test", reason=alias, defer_to_commit=False)
            return read_alias(active_relationship_model())

    async def run():
        return await asyncio.gather(operation("default"), operation("selected"))

    with database_context("selected"):
        with pytest.raises(ValueError), database_context("default"):
            raise ValueError("restore")
        assert read_alias(active_relationship_model()) == "selected"
        assert asyncio.run(run()) == ["default", "selected"]
        assert read_alias(active_relationship_model()) == "selected"
    assert read_alias(active_relationship_model()) == "default"
    assert PermissionAuditEvent.objects.using("selected").get().reason == "selected"
    assert PermissionAuditEvent.objects.using("default").get().reason == "default"


def test_sync_prune_explicit_database_does_not_follow_default_router():
    with override_settings(DATABASE_ROUTERS=[DefaultRouter()]):
        call_command("rebac", "sync", database="selected", prune_stale=True, yes=True, verbosity=0)
    assert SchemaDefinition.objects.using("selected").filter(resource_type="blog/post").exists()
    assert not SchemaDefinition.objects.using("default").exists()


def test_manual_policy_decisions_do_not_cross_aliases():
    from rebac.testing import install_schema

    local = install_schema(
        """
        definition auth/user {}
        definition demo/item {
            relation owner: auth/user
            permission read = owner
        }
        """,
        using="selected",
    )
    with database_context("selected"):
        write_relationships([RelationshipTuple(RESOURCE, "owner", ACTOR)])
    with evaluator_scope() as evaluator:
        assert not evaluator.check(local, subject=ACTOR, action="read", resource=RESOURCE).allowed
        with database_context("selected"):
            assert evaluator.check(local, subject=ACTOR, action="read", resource=RESOURCE).allowed
        assert not evaluator.check(local, subject=ACTOR, action="read", resource=RESOURCE).allowed


def test_explicit_queryset_redaction_including_async_iterator_uses_its_database(settings):
    settings.REBAC_FIELD_READ_MODE = "redact"
    policy("default", resource_type="blog/post")
    policy("selected", resource_type="blog/post")
    for alias, expression in (("default", "nil"), ("selected", "owner")):
        definition = SchemaDefinition.objects.using(alias).get(resource_type="blog/post")
        SchemaPermission.objects.using(alias).create(
            definition=definition, name="read__title", expression=expression
        )
    with database_context("default"), system_context(reason="fixture"):
        post = Post.objects.using("default").create(title="must redact")
        write_relationships(
            [RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "owner", ACTOR)]
        )
    with database_context("selected"):
        assert Post.objects.using("default").with_actor(ACTOR).get(pk=post.pk).title is None

        async def titles():
            return [
                row.title
                async for row in Post.objects.using("default").with_actor(ACTOR).aiterator()
            ]

        assert asyncio.run(titles()) == [None]


def test_audit_explicit_alias_overrides_scope():
    with database_context("selected"):
        emit("test", reason="explicit", using="default")
    assert PermissionAuditEvent.objects.using("default").get().reason == "explicit"
    assert not PermissionAuditEvent.objects.using("selected").exists()


def test_unscoped_instance_alias_preserves_native_router_precedence():
    from rebac.database import write_alias

    post = Post(title="router")
    post._state.db = "selected"
    with override_settings(DATABASE_ROUTERS=[DefaultRouter()]):
        assert read_alias(Post, instance=post) == "default"
        assert write_alias(Post, instance=post) == "default"
        with database_context("default"):
            assert read_alias(Post, instance=post) == "selected"
            assert write_alias(Post, instance=post, using="default") == "default"


def test_bound_instance_redaction_discovers_gates_on_its_database():
    policy("default", resource_type="blog/post")
    policy("selected", resource_type="blog/post")
    definition = SchemaDefinition.objects.using("default").get(resource_type="blog/post")
    SchemaPermission.objects.using("default").create(
        definition=definition, name="read__title", expression="nil"
    )
    post = Post(id=1, title="secret").with_actor(ACTOR)
    post._state.db = "default"
    post._state.adding = False
    with database_context("selected"):
        assert post.denied_read_fields() == frozenset({"title"})
        assert post.redacted(mode="redact").title is None


@pytest.mark.parametrize(
    "operation", ["save", "delete", "check_save", "save_bypass", "delete_bypass"]
)
def test_explicit_instance_write_audits_keep_operation_alias(settings, operation):
    from rebac.errors import PermissionDenied

    settings.REBAC_AUDIT_DENIALS = True
    for alias in ("default", "selected"):
        policy(alias, resource_type="blog/post")
        definition = SchemaDefinition.objects.using(alias).get(resource_type="blog/post")
        for action in ("write", "delete"):
            SchemaPermission.objects.using(alias).create(
                definition=definition, name=action, expression="nil"
            )
        with database_context(alias), system_context(reason="fixture"):
            Post.objects.using(alias).create(id=1, title="unchanged")
        PermissionAuditEvent.objects.using(alias).all().delete()
    post = Post.objects.using("default").sudo(reason="fixture read").get(pk=1)
    PermissionAuditEvent.objects.using("default").all().delete()
    post.unsudo().with_actor(ACTOR)
    if operation == "check_save":
        assert post.sudo(reason="explicit check").check_save(using="selected").allowed
    elif operation == "save_bypass":
        post.sudo(reason="explicit save").save_base(using="selected")
    elif operation == "delete_bypass":
        post.sudo(reason="explicit delete").delete(using="selected")
    else:
        with pytest.raises(PermissionDenied):
            if operation == "save":
                post.save_base(using="selected")
            else:
                post.delete(using="selected")
    if operation != "save_bypass":
        assert post._state.db == "default"
    assert PermissionAuditEvent.objects.using("selected").count() == 1
    assert not PermissionAuditEvent.objects.using("default").exists()


def test_selected_related_bounded_checks_keep_the_queryset_alias(monkeypatch):
    from django.db import connections
    from django.test.utils import CaptureQueriesContext

    from rebac import actor_context
    from tests.testapp.models import Folder

    for alias in ("default", "selected"):
        policy(alias, resource_type="blog/folder")
        policy(alias, resource_type="blog/post")
        with database_context(alias), system_context(reason="bounded related fixtures"):
            folder = Folder.objects.using(alias).create(pk=771, name=alias)
            Post.objects.using(alias).create(pk=772, title=alias, folder=folder)
            write_relationships(
                [
                    RelationshipTuple(ObjectRef("blog/post", "772"), "owner", ACTOR),
                    *(
                        [RelationshipTuple(ObjectRef("blog/folder", "771"), "owner", ACTOR)]
                        if alias == "selected"
                        else []
                    ),
                ]
            )
    active = backend()
    original = active.held
    observed = []

    def scoped(**kwargs):
        if kwargs["resource_type"] != "blog/folder":
            return original(**kwargs)
        using = kwargs["using"]
        other = "default" if using == "selected" else "selected"
        with (
            CaptureQueriesContext(connections[using]) as queries,
            CaptureQueriesContext(connections[other]) as other_queries,
        ):
            result = original(**kwargs)
        observed.append((using, [query["sql"] for query in queries], len(other_queries)))
        return result

    monkeypatch.setattr(active, "held", scoped)
    with database_context("default"), actor_context(ACTOR):
        row = Post.objects.using("selected").rebac_select_related("folder").get(pk=772)
        assert row.folder.name == "selected"
        from rebac import PermissionDenied

        with pytest.raises(PermissionDenied):
            Post.objects.using("default").rebac_select_related("folder").get(pk=772)
    assert [alias for alias, _sql, _other_count in observed] == ["selected", "default"]
    assert all(other_count == 0 for _alias, _sql, other_count in observed)
    assert all(
        any(
            "SELECT" in statement and "rebac_relationship" in statement.lower() for statement in sql
        )
        for _alias, sql, _other_count in observed
    )
