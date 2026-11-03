"""Point statements preserve native source filter and edge semantics."""

from __future__ import annotations

import json

import pytest
from django.db import connection, models
from django.test import override_settings
from django.test.utils import isolate_apps

from rebac import (
    ObjectRef,
    RebacMixin,
    RelationshipTuple,
    SubjectRef,
    backend,
    database_context,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from rebac.compile import read
from rebac.errors import SchemaError
from tests.backend_setup import install_schema
from tests.testapp.models import BackingQueue, BackingTask, Folder, Post

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pg_delta]
ALICE = SubjectRef.of("auth/user", "point-filter-alice")
BOB = SubjectRef.of("auth/user", "point-filter-bob")
SCHEMA = """
definition auth/user {}
definition blog/post {
    relation viewer: auth/user
    permission read = viewer
}
definition blog/folder {
    relation viewer: auth/user
    relation posts: blog/post // rebac:field={"path":"posts","filters":{"is_active":true}}
    permission read = viewer + posts->read
    permission exclusion = viewer - posts->read
}
"""


def test_fixed_fact_revoked_after_decision_fails_point_witness(local, monkeypatch):
    install_schema(
        local,
        """
        definition auth/user {}
        definition site/role {
            relation member: auth/user
        }
        definition blog/post {
            relation admin: site/role // rebac:const=admin
            permission read = admin->member
        }
    """,
    )
    with sudo(reason="fixed fact fixture"):
        post = Post.objects.create(title="fixed fact")
    grant = RelationshipTuple(ObjectRef("site/role", "admin"), "member", ALICE)
    local.write_relationships([grant])
    original = read._Facts._decide
    changed = []

    def decide_then_revoke(self, fact):
        found = original(self, fact)
        if not changed:
            changed.append(True)
            local.delete_relationship(grant)
        return found

    monkeypatch.setattr(read._Facts, "_decide", decide_then_revoke)
    assert not local.check_access(
        subject=ALICE, action="read", resource=to_object_ref(post)
    ).allowed
    assert changed


@pytest.fixture(params=["denormalized", "registry"])
def local(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        active = backend()
        install_schema(active, SCHEMA)
        yield active
        reset_backend()


def test_matching_multi_target_arrow_and_clone_keep_live_actor_outcomes(local):
    with sudo(reason="tests.point_filter.matching_fixture"):
        folder = Folder.objects.create(name="active", is_active=True)
        first = Post.objects.create(title="first", folder=folder)
        second = Post.objects.create(title="second", folder=folder)
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(first), "viewer", ALICE),
            RelationshipTuple(to_object_ref(second), "viewer", BOB),
        ]
    )
    query = Folder.objects.with_actor(ALICE).filter(pk=folder.pk)
    assert query.exists()
    assert query.all().exists()
    assert query.with_actor(BOB).exists()
    with sudo(reason="tests.point_filter.disable_source"):
        Folder.objects.filter(pk=folder.pk).update(is_active=False)
    assert not query.all().exists()
    assert not query.with_actor(BOB).exists()


@pytest.mark.parametrize(
    "filters", [{"name__iexact": "INACTIVE"}, {"kind": "PUBLIC"}, {"parent_id": None}]
)
def test_local_transforms_custom_preparation_and_null_follow_stored_query(local, filters):
    text = SCHEMA.replace('"filters":{"is_active":true}', '"filters":' + json.dumps(filters))
    install_schema(local, text)
    with sudo(reason="tests.point_filter.lookup_fixture"):
        folder = Folder.objects.create(name="inactive", kind="public", parent=None)
        other = Folder.objects.create(name="other", kind="private", parent=folder)
        first = Post.objects.create(title="first", folder=folder)
        second = Post.objects.create(title="second", folder=other)
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(first), "viewer", ALICE),
            RelationshipTuple(to_object_ref(second), "viewer", ALICE),
        ]
    )
    assert Folder.objects.with_actor(ALICE).filter(pk=folder.pk).exists()
    assert not Folder.objects.with_actor(ALICE).filter(pk=other.pk).exists()
    assert not Folder.objects.with_actor(BOB).filter(pk=folder.pk).exists()


@pytest.mark.parametrize("fact", ["filter", "edge"])
@pytest.mark.django_db(transaction=True, databases={"default", "selected"})
def test_source_match_alias_and_clone_keep_opposite_rows_separate(local, fact):
    for alias in ("default", "selected"):
        with database_context(alias):
            install_schema(local, SCHEMA, using=alias)
            with sudo(reason="tests.point_filter.alias_fixture"):
                folder = Folder.objects.using(alias).create(
                    pk=71, name=alias, is_active=fact == "edge" or alias == "default"
                )
                post = Post.objects.using(alias).create(
                    pk=81,
                    title=alias,
                    folder=folder if fact == "filter" or alias == "default" else None,
                )
            local.write_relationships(
                [
                    RelationshipTuple(to_object_ref(folder), "viewer", ALICE),
                    RelationshipTuple(to_object_ref(post), "viewer", ALICE),
                ]
            )
    query = (
        Folder.objects.using("selected").with_actor(ALICE).with_action("exclusion").filter(pk=71)
    )
    assert query.exists()
    assert not query.using("default").exists()
    assert not query.with_actor(BOB).exists()
    with database_context("selected"):
        if fact == "filter":
            Folder.objects.using("selected").sudo(reason="tests.point_filter.alias_change").filter(
                pk=71
            ).update(is_active=True)
        else:
            Post.objects.using("selected").sudo(reason="tests.point_source.alias_link").filter(
                pk=81
            ).update(folder_id=71)
    assert not query.all().exists()


@pytest.fixture(params=[True])
def scalar_source(local, monkeypatch, request):
    # Follow the existing isolated-model harness used by test_scope_additive:
    # only the native resource resolver needs the test-local app registry.
    import rebac.resources

    with isolate_apps("tests.testapp"):

        class ScalarSource(RebacMixin, models.Model):
            target = models.ForeignKey(
                Folder, null=True, db_constraint=request.param, on_delete=models.CASCADE
            )
            metadata = models.JSONField(null=True)
            audience = models.CharField(
                max_length=32, db_collation="nocase" if connection.vendor == "sqlite" else None
            )

            class Meta:
                app_label = "testapp"
                rebac_resource_type = "test/pointscalar"

        original_types = rebac.resources._model_types
        monkeypatch.setattr(
            rebac.resources,
            "_model_types",
            lambda prefix: (
                *original_types(prefix),
                *(
                    (f"{prefix}{model._meta.rebac_resource_type}", model)
                    for model in (ScalarSource,)
                ),
            ),
        )
        with connection.schema_editor() as editor:
            editor.create_model(ScalarSource)
        try:
            yield ScalarSource
        finally:
            with connection.schema_editor() as editor:
                editor.delete_model(ScalarSource)


def scalar_schema(filters):
    return (
        SCHEMA
        + """
    definition test/pointscalar {
        relation viewer: auth/user
        relation target: blog/folder // rebac:field="""
        + json.dumps({"path": "target", "filters": filters})
        + """
        permission read = target->read
        permission exclusion = viewer - target->read
    }
    """
    )


def test_source_filter_uses_stored_column_collation(local, scalar_source):
    if connection.vendor != "sqlite":
        pytest.skip("SQLite's native named nocase collation")
    install_schema(local, scalar_schema({"audience": "PUBLIC"}))
    with sudo(reason="tests.point_filter.collation_fixture"):
        target = Folder.objects.create(name="shared")
        public = scalar_source.objects.create(target=target, metadata=None, audience="public")
        private = scalar_source.objects.create(target=target, metadata=None, audience="private")
    local.write_relationships([RelationshipTuple(to_object_ref(target), "viewer", ALICE)])
    assert scalar_source.objects.with_actor(ALICE).filter(pk=public.pk).exists()
    assert not scalar_source.objects.with_actor(ALICE).filter(pk=private.pk).exists()


def test_invalid_field_filter_is_not_swallowed_as_an_optimization_miss(local):
    with sudo(reason="tests.point_filter.invalid_fixture"):
        folder = Folder.objects.create(name="source")
    invalid = SCHEMA.replace('"is_active":true', '"missing_column":true')
    with pytest.raises((SchemaError, ValueError)):
        install_schema(local, invalid)
        local.check_access(subject=ALICE, action="read", resource=to_object_ref(folder))


@pytest.mark.parametrize("edge", ["reverse", "nullable"])
@pytest.mark.parametrize("operation", ["check", "exact_scope"])
def test_absent_source_edge_preserves_point_outcome(local, edge, operation):
    if edge == "reverse":
        text = SCHEMA.replace('{"path":"posts","filters":{"is_active":true}}', "posts")
        source_model = Folder
    else:
        text = """
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            permission read = viewer
        }
        definition blog/post {
            relation viewer: auth/user
            relation folder: blog/folder // rebac:field=folder
            permission read = viewer + folder->read
        }
        """
        source_model = Post
    install_schema(local, text)
    with sudo(reason="tests.point_source.absent_fixture"):
        source = (
            Folder.objects.create(name="empty")
            if edge == "reverse"
            else Post.objects.create(title="null")
        )
    local.write_relationships([RelationshipTuple(to_object_ref(source), "viewer", ALICE)])

    def allowed(actor):
        if operation == "check":
            return local.check_access(
                subject=actor, action="read", resource=to_object_ref(source)
            ).allowed
        return source_model.objects.with_actor(actor).filter(pk=source.pk).exists()

    assert allowed(ALICE)
    assert not allowed(BOB)
    with sudo(reason="tests.point_source.add_target"):
        if edge == "reverse":
            target = Post.objects.create(title="target", folder=source)
        else:
            target = Folder.objects.create(name="target")
            Post.objects.filter(pk=source.pk).update(folder=target)
    local.write_relationships([RelationshipTuple(to_object_ref(target), "viewer", ALICE)])
    assert allowed(ALICE)
    assert not allowed(BOB)


def test_fk_attname_filter_follows_parent_name(local):
    install_schema(
        local,
        SCHEMA.replace('"filters":{"is_active":true}', '"filters":{"parent_id__name":"eligible"}'),
    )
    with sudo(reason="point filter fixture"):
        parent = Folder.objects.create(name="eligible")
        folder = Folder.objects.create(name="child", parent=parent)
        post = Post.objects.create(title="target", folder=folder)
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", ALICE)])
    assert Folder.objects.with_actor(ALICE).filter(pk=folder.pk).exists()
    with sudo(reason="point filter change"):
        Folder.objects.filter(pk=parent.pk).update(name="not eligible")
    assert not Folder.objects.with_actor(ALICE).filter(pk=folder.pk).exists()


@pytest.mark.parametrize("filters", [{"metadata": None}, {"metadata__flag": None}])
def test_json_sql_null_and_key_null_keep_native_point_truth(local, scalar_source, filters):
    install_schema(local, scalar_schema(filters))
    with sudo(reason="point filter JSON fixture"):
        target = Folder.objects.create(name="shared")
        rows = [
            scalar_source.objects.create(target=target, metadata=value, audience="unused")
            for value in (None, {"flag": None}, {"flag": 1})
        ]
    local.write_relationships(
        [RelationshipTuple(to_object_ref(target), "viewer", ALICE)]
        + [RelationshipTuple(to_object_ref(row), "viewer", ALICE) for row in rows]
    )
    expected = [True, False, False] if "metadata" in filters else [False, True, False]
    for row, matches in zip(rows, expected, strict=True):
        assert scalar_source.objects.with_actor(ALICE).filter(pk=row.pk).exists() is matches
        assert (
            local.check_access(
                subject=ALICE, action="exclusion", resource=to_object_ref(row)
            ).allowed
            is not matches
        )


def test_multi_valued_filter_preserves_same_related_row(local):
    install_schema(
        local,
        SCHEMA.replace('"filters":{"is_active":true}', '"filters":{"posts__title":"included"}'),
    )
    with sudo(reason="point filter multi fixture"):
        folder = Folder.objects.create(name="source")
        included = Post.objects.create(title="included", folder=folder)
        excluded = Post.objects.create(title="excluded", folder=folder)
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(included), "viewer", BOB),
            RelationshipTuple(to_object_ref(excluded), "viewer", ALICE),
        ]
    )
    assert not Folder.objects.with_actor(ALICE).filter(pk=folder.pk).exists()
    assert Folder.objects.with_actor(BOB).filter(pk=folder.pk).exists()


def test_nonunique_source_identity_keeps_native_population(local, monkeypatch):
    with sudo(reason="point filter nonunique fixture"):
        identity = Folder.objects.create(name="shared identity")
        target = Folder.objects.create(name="allowed")
        first = Post.objects.create(title="linked", folder=identity)
        Post.objects.create(title="empty", folder=identity)
        first.collections.add(target)
    monkeypatch.setattr(Post._meta, "rebac_id_attr", "folder_id", raising=False)
    install_schema(
        local,
        """
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            permission read = viewer
        }
        definition blog/post {
            relation collections: blog/folder // rebac:field=collections
            permission read = collections->read
        }
        """,
    )
    local.write_relationships([RelationshipTuple(to_object_ref(target), "viewer", ALICE)])
    assert local.check_access(
        subject=ALICE, action="read", resource=ObjectRef("blog/post", str(identity.pk))
    ).allowed


def test_nested_exclusion_children_ban_survives_root_absence(local):
    install_schema(
        local,
        """
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            relation parent: blog/folder // rebac:field=parent
            relation children: blog/folder // rebac:field=children
            permission ban = parent->viewer
            permission read = viewer - children->ban
        }
        """,
    )
    with sudo(reason="point filter nested fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
    resource = to_object_ref(root)
    local.write_relationships([RelationshipTuple(resource, "viewer", ALICE)])
    assert not local.check_access(subject=ALICE, action="read", resource=resource).allowed
    assert not Folder.objects.with_actor(ALICE).filter(pk=root.pk).exists()
    with sudo(reason="point filter remove nested edge"):
        child.delete()
    assert local.check_access(subject=ALICE, action="read", resource=resource).allowed
    assert Folder.objects.with_actor(ALICE).filter(pk=root.pk).exists()


@pytest.mark.parametrize("filters", [{}, {"visibility": "inherited"}])
@pytest.mark.parametrize("operation", ["check", "exact_scope"])
def test_mandatory_direct_pointer_and_virtual_tuple(local, filters, operation):
    install_schema(
        local,
        """
        definition auth/user {}
        definition test/backingqueue {
            relation viewer: auth/user
            permission read = viewer
        }
        definition test/backingtask {
            relation viewer: auth/user
            relation queue: test/backingqueue // rebac:field="""
        + json.dumps({"path": "queue", "filters": filters})
        + """
            permission read = viewer + queue->read
        }
        """,
    )
    with sudo(reason="point filter mandatory fixture"):
        target = BackingQueue.objects.create()
        source = BackingTask.objects.create(queue=target, visibility="record")
    grants = [RelationshipTuple(to_object_ref(target), "viewer", ALICE)]
    if filters:
        grants.append(RelationshipTuple(to_object_ref(source), "viewer", ALICE))
    local.write_relationships(grants)
    for actor, expected in ((ALICE, True), (BOB, False)):
        if operation == "check":
            allowed = local.check_access(
                subject=actor, action="read", resource=to_object_ref(source)
            ).allowed
        else:
            allowed = BackingTask.objects.with_actor(actor).filter(pk=source.pk).exists()
        assert allowed is expected
    virtual = ObjectRef("test/backingtask", "9999999")
    local.write_relationships([RelationshipTuple(virtual, "viewer", ALICE)])
    assert local.check_access(subject=ALICE, action="read", resource=virtual).allowed
    assert not BackingTask.objects.with_actor(ALICE).filter(pk=9999999).exists()
