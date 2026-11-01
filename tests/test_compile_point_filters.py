"""Point statements prune impossible source edges and witness their absence."""

from __future__ import annotations

import json

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection, connections, models
from django.test import override_settings
from django.test.utils import CaptureQueriesContext, isolate_apps

from rebac import (
    CheckItem,
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
from rebac.compile import Compiler, read
from rebac.errors import SchemaError
from tests.backend_setup import install_schema
from tests.testapp.models import Attachment, Folder, Post

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


@pytest.fixture(params=["denormalized", "registry"])
def local(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        active = backend()
        install_schema(active, SCHEMA)
        yield active
        reset_backend()


def test_nonmatching_source_filter_never_compiles_target(local, monkeypatch):
    with sudo(reason="tests.point_filter.fixture"):
        folder = Folder.objects.create(name="inactive", is_active=False)
        Post.objects.create(title="target", folder=folder)
    local.write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", ALICE)])
    original = Compiler._subject_rows
    targets = []

    def subject_rows(self, allowed_type, *args, **kwargs):
        if allowed_type == "blog/post":
            targets.append(allowed_type)
        return original(self, allowed_type, *args, **kwargs)

    monkeypatch.setattr(Compiler, "_subject_rows", subject_rows)
    assert Folder.objects.with_actor(ALICE).filter(pk=folder.pk).exists()
    assert targets == []


@pytest.fixture
def metadata_reads(monkeypatch):
    original = read._point_rows
    observed = []

    def point_rows(using, resource_type, requirements, ids):
        with CaptureQueriesContext(connections[using]) as queries:
            data = original(using, resource_type, requirements, ids)
        observed.append(
            {"using": using, "type": resource_type, "queries": len(queries), "data": data}
        )
        return data

    monkeypatch.setattr(read, "_point_rows", point_rows)
    return observed


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


@pytest.mark.parametrize("operation", ["check", "exact_scope"])
def test_false_to_true_filter_change_cannot_grant_through_exclusion(local, monkeypatch, operation):
    with sudo(reason="tests.point_filter.race_fixture"):
        folder = Folder.objects.create(name="inactive", is_active=False)
        post = Post.objects.create(title="ban", folder=folder)
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(folder), "viewer", ALICE),
            RelationshipTuple(to_object_ref(post), "viewer", ALICE),
        ]
    )
    original = read._point_rows
    changed = []

    def stale(using, resource_type, requirements, ids):
        data = original(using, resource_type, requirements, ids)
        if resource_type == "blog/folder" and not changed:
            assert any(not held for row in data.values() for _key, held in row.source_matches)
            changed.append(True)
            Folder.objects.using(using).sudo(reason="tests.point_filter.race").filter(
                pk=folder.pk
            ).update(is_active=True)
        return data

    monkeypatch.setattr(read, "_point_rows", stale)
    if operation == "check":
        allowed = local.check_access(
            subject=ALICE, action="exclusion", resource=to_object_ref(folder)
        ).allowed
    else:
        allowed = (
            Folder.objects.with_actor(ALICE).with_action("exclusion").filter(pk=folder.pk).exists()
        )
    assert not allowed
    assert changed


@pytest.mark.parametrize("grant_kind", ["tuple", "constant"])
def test_filter_only_metadata_does_not_require_deleted_source(local, monkeypatch, grant_kind):
    if grant_kind == "constant":
        text = SCHEMA.replace(
            "definition auth/user {}",
            "definition auth/user {}\ndefinition site/role { relation member: auth/user }",
        )
        text = text.replace(
            "definition blog/folder {",
            "definition blog/folder {\n relation public: site/role // rebac:const=public",
        )
        text = text.replace(
            "permission read = viewer + posts->read",
            "permission read = public->member + posts->read",
        )
        install_schema(local, text)
        local.write_relationships(
            [RelationshipTuple(ObjectRef("site/role", "public"), "member", ALICE)]
        )
    with sudo(reason="tests.point_filter.deletion_fixture"):
        folder = Folder.objects.create(name="deleted", is_active=False)
    resource = to_object_ref(folder)
    original = read._point_rows
    deleted = []

    def remove_source(using, resource_type, requirements, ids):
        data = original(using, resource_type, requirements, ids)
        if resource_type == "blog/folder" and not deleted:
            assert data[resource.resource_id].types == ()
            assert data[resource.resource_id].source_matches
            deleted.append(True)
            Folder.objects.using(using).sudo(reason="tests.point_filter.delete").filter(
                pk=folder.pk
            ).delete()
            if grant_kind == "tuple":
                # Deleted resource tuples are cleaned up by their native owner;
                # this is a fresh grant to the now tuple-only identity.
                local.write_relationships([RelationshipTuple(resource, "viewer", ALICE)])
        return data

    monkeypatch.setattr(read, "_point_rows", remove_source)
    assert local.check_access(subject=ALICE, action="read", resource=resource).allowed
    assert deleted
    assert not Folder._base_manager.filter(pk=folder.pk).exists()


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


def test_fk_attname_traversal_is_not_mistaken_for_local_scalar_filter(
    local, monkeypatch, metadata_reads
):
    text = SCHEMA.replace(
        '"filters":{"is_active":true}', '"filters":{"parent_id__name":"eligible"}'
    )
    install_schema(local, text)
    with sudo(reason="tests.point_filter.join_fixture"):
        parent = Folder.objects.create(name="eligible")
        folder = Folder.objects.create(name="child", parent=parent)
        post = Post.objects.create(title="target", folder=folder)
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", ALICE)])
    original = Compiler._subject_rows
    targets = []

    def subject_rows(self, allowed_type, *args, **kwargs):
        targets.append(allowed_type)
        return original(self, allowed_type, *args, **kwargs)

    monkeypatch.setattr(Compiler, "_subject_rows", subject_rows)
    metadata_reads.clear()
    assert Folder.objects.with_actor(ALICE).filter(pk=folder.pk).exists()
    assert "blog/post" in targets
    assert all(row["queries"] == 0 for row in metadata_reads if row["type"] == "blog/folder")
    with sudo(reason="tests.point_filter.join_change"):
        Folder.objects.filter(pk=parent.pk).update(name="not eligible")
    assert not Folder.objects.with_actor(ALICE).filter(pk=folder.pk).exists()


def test_type_and_filter_facts_share_one_metadata_read(local, metadata_reads):
    text = (
        SCHEMA
        + """
    definition test/attachment {
        relation viewer: auth/user
        relation folder: blog/folder // rebac:field={"path":"target","filters":{"label":"visible"}}
        relation post: blog/post // rebac:field=target
        permission read = viewer + folder->read + post->read
    }
    """
    )
    install_schema(local, text)
    with sudo(reason="tests.point_filter.generic_fixture"):
        folder = Folder.objects.create(name="target")
        attachment = Attachment.objects.create(
            content_type=ContentType.objects.get_for_model(Folder),
            object_id=folder.pk,
            label="hidden",
        )
    local.write_relationships([RelationshipTuple(to_object_ref(attachment), "viewer", ALICE)])
    metadata_reads.clear()
    assert Attachment.objects.with_actor(ALICE).filter(pk=attachment.pk).exists()
    own = [row for row in metadata_reads if row["type"] == "test/attachment"]
    assert len(own) == 1 and own[0]["queries"] == 1
    data = own[0]["data"][str(attachment.pk)]
    assert data.types == (("content_type_id", attachment.content_type_id),)
    assert data.source_matches == (
        (("test/attachment", "folder"), False),
        (("test/attachment", "post"), True),
    )


def test_bulk_source_metadata_groups_subjects_actions_and_resource_ids(local, metadata_reads):
    with sudo(reason="tests.point_filter.bulk_fixture"):
        folders = [Folder.objects.create(name=str(i), is_active=False) for i in range(10)]
    local.write_relationships(
        [RelationshipTuple(to_object_ref(row), "viewer", ALICE) for row in folders]
    )
    for count in (1, 10):
        metadata_reads.clear()
        items = [
            CheckItem(subject=actor, action=action, resource=to_object_ref(row))
            for row in folders[:count]
            for actor in (ALICE, BOB)
            for action in ("read", "exclusion")
        ]
        replies = local.check_bulk_permissions(items)
        assert [reply.allowed for reply in replies] == [item.subject == ALICE for item in items]
        own = [row for row in metadata_reads if row["type"] == "blog/folder"]
        assert len(own) == 1 and own[0]["queries"] == 1
        assert len(own[0]["data"]) == count


def test_permission_without_field_backings_adds_no_metadata_read(local, metadata_reads):
    with sudo(reason="tests.point_filter.unfiltered_fixture"):
        folder = Folder.objects.create(name="viewer")
    local.write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", ALICE)])
    metadata_reads.clear()
    assert local.check_access(
        subject=ALICE, action="viewer", resource=to_object_ref(folder)
    ).allowed
    assert all(row["queries"] == 0 for row in metadata_reads)


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
    import rebac.compile.predicate
    import rebac.field_backing
    import rebac.resources
    import rebac.watch

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

        original = rebac.resources.model_for_resource_type
        for module in (
            rebac.resources,
            rebac.field_backing,
            rebac.watch,
            rebac.compile.predicate,
            read,
        ):
            monkeypatch.setattr(
                module,
                "model_for_resource_type",
                lambda name: ScalarSource if name == "test/pointscalar" else original(name),
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


@pytest.mark.parametrize("filters", [{"metadata": None}, {"metadata__flag": None}])
def test_json_sql_null_and_key_null_keep_native_point_truth(
    local, scalar_source, metadata_reads, filters
):
    install_schema(local, scalar_schema(filters))
    with sudo(reason="tests.point_filter.json_fixture"):
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
        metadata_reads.clear()
        assert scalar_source.objects.with_actor(ALICE).filter(pk=row.pk).exists() is matches
        own = [item for item in metadata_reads if item["type"] == "test/pointscalar"]
        assert len(own) == 1 and own[0]["queries"] == 1
        assert own[0]["data"][str(row.pk)].source_matches == (
            (("test/pointscalar", "target"), matches),
        )
        assert (
            local.check_access(
                subject=ALICE, action="exclusion", resource=to_object_ref(row)
            ).allowed
            is not matches
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


def test_metadata_preserves_filter_scalar_types_instead_of_python_equality(local, metadata_reads):
    text = SCHEMA.replace('"filters":{"is_active":true}', '"filters":{"name":true}')
    text = text.replace(
        "permission read = viewer + posts->read",
        'relation numeric: blog/post // rebac:field={"path":"posts","filters":{"name":1}}\n permission read = viewer + posts->read + numeric->read',
    )
    install_schema(local, text)
    with sudo(reason="tests.point_filter.scalar_types_fixture"):
        rows = [Folder.objects.create(name=name) for name in ("True", "1")]
        posts = [Post.objects.create(title="target", folder=row) for row in rows]
    local.write_relationships(
        [RelationshipTuple(to_object_ref(post), "viewer", ALICE) for post in posts]
    )
    for row, expected in zip(rows, [(False, True), (True, False)], strict=True):
        metadata_reads.clear()
        assert Folder.objects.with_actor(ALICE).filter(pk=row.pk).exists()
        data = next(item for item in metadata_reads if item["type"] == "blog/folder")["data"][
            str(row.pk)
        ]
        assert tuple(held for _key, held in data.source_matches) == expected


def test_point_preparation_chunks_large_identity_population(local):
    with sudo(reason="tests.point_filter.chunk_fixture"):
        rows = Folder.objects.bulk_create(
            [Folder(name=str(i), is_active=bool(i % 2)) for i in range(501)]
        )
        Post.objects.bulk_create([Post(title="target", folder=row) for row in rows])
    policy = read._policy(local, "default")
    with CaptureQueriesContext(connection) as queries:
        data = read._point_data(
            policy, "default", ("blog/folder", "read"), [str(row.pk) for row in rows]
        )
    assert len(queries) == 2
    assert len(data) == 501
    for row in rows:
        assert data[str(row.pk)].source_matches == ((("blog/folder", "posts"), row.is_active),)


def test_unfiltered_present_edge_uses_one_metadata_read(local, metadata_reads):
    install_schema(
        local, SCHEMA.replace('field={"path":"posts","filters":{"is_active":true}}', "field=posts")
    )
    with sudo(reason="tests.point_filter.empty_fixture"):
        folder = Folder.objects.create(name="inactive", is_active=False)
        post = Post.objects.create(title="shared", folder=folder)
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", ALICE)])
    metadata_reads.clear()
    assert Folder.objects.with_actor(ALICE).filter(pk=folder.pk).exists()
    own = [item for item in metadata_reads if item["type"] == "blog/folder"]
    assert len(own) == 1 and own[0]["queries"] == 1
    assert own[0]["data"][str(folder.pk)].source_matches == ((("blog/folder", "posts"), True),)


def test_invalid_field_filter_is_not_swallowed_as_an_optimization_miss(local):
    with sudo(reason="tests.point_filter.invalid_fixture"):
        folder = Folder.objects.create(name="source")
    invalid = SCHEMA.replace('"is_active":true', '"missing_column":true')
    with pytest.raises((SchemaError, ValueError)):
        install_schema(local, invalid)
        local.check_access(subject=ALICE, action="read", resource=to_object_ref(folder))


def test_multi_valued_filter_fallback_preserves_same_related_row(local, metadata_reads):
    text = SCHEMA.replace('"filters":{"is_active":true}', '"filters":{"posts__title":"included"}')
    install_schema(local, text)
    with sudo(reason="tests.point_filter.multi_fixture"):
        folder = Folder.objects.create(name="source")
        included = Post.objects.create(title="included", folder=folder)
        excluded = Post.objects.create(title="excluded", folder=folder)
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(included), "viewer", BOB),
            RelationshipTuple(to_object_ref(excluded), "viewer", ALICE),
        ]
    )
    metadata_reads.clear()
    assert not Folder.objects.with_actor(ALICE).filter(pk=folder.pk).exists()
    assert Folder.objects.with_actor(BOB).filter(pk=folder.pk).exists()
    assert all(item["queries"] == 0 for item in metadata_reads)


@pytest.mark.parametrize("operation", ["check", "exact_scope"])
def test_matching_filter_still_reads_original_condition_at_execution(local, monkeypatch, operation):
    with sudo(reason="tests.point_filter.matching_race_fixture"):
        folder = Folder.objects.create(name="active", is_active=True)
        post = Post.objects.create(title="target", folder=folder)
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", ALICE)])
    original = read._point_rows
    changed = []

    def stale(using, resource_type, requirements, ids):
        data = original(using, resource_type, requirements, ids)
        if resource_type == "blog/folder" and not changed:
            assert all(held for row in data.values() for _key, held in row.source_matches)
            changed.append(True)
            Folder.objects.using(using).sudo(reason="tests.point_filter.matching_race").filter(
                pk=folder.pk
            ).update(is_active=False)
        return data

    monkeypatch.setattr(read, "_point_rows", stale)
    if operation == "check":
        allowed = local.check_access(
            subject=ALICE, action="read", resource=to_object_ref(folder)
        ).allowed
    else:
        allowed = Folder.objects.with_actor(ALICE).filter(pk=folder.pk).exists()
    assert not allowed
    assert changed


@pytest.mark.parametrize("edge", ["reverse", "nullable"])
@pytest.mark.parametrize("operation", ["check", "exact_scope"])
def test_absent_source_edge_never_compiles_target(local, monkeypatch, edge, operation):
    if edge == "reverse":
        text = SCHEMA.replace('{"path":"posts","filters":{"is_active":true}}', "posts")
        source_model, target_type = Folder, "blog/post"
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
        source_model, target_type = Post, "blog/folder"
    install_schema(local, text)
    with sudo(reason="tests.point_source.absent_fixture"):
        source = (
            Folder.objects.create(name="empty")
            if edge == "reverse"
            else Post.objects.create(title="null")
        )
    local.write_relationships([RelationshipTuple(to_object_ref(source), "viewer", ALICE)])
    original = Compiler._holds
    targets = []

    def holds(self, key, *args, **kwargs):
        if key == (target_type, "read"):
            targets.append(key)
        return original(self, key, *args, **kwargs)

    monkeypatch.setattr(Compiler, "_holds", holds)

    def allowed(actor):
        if operation == "check":
            return local.check_access(
                subject=actor, action="read", resource=to_object_ref(source)
            ).allowed
        return source_model.objects.with_actor(actor).filter(pk=source.pk).exists()

    assert allowed(ALICE)
    assert not allowed(BOB)
    assert targets == []
    with sudo(reason="tests.point_source.add_target"):
        if edge == "reverse":
            target = Post.objects.create(title="target", folder=source)
        else:
            target = Folder.objects.create(name="target")
            Post.objects.filter(pk=source.pk).update(folder=target)
    local.write_relationships([RelationshipTuple(to_object_ref(target), "viewer", ALICE)])
    targets.clear()
    assert allowed(ALICE)
    assert not allowed(BOB)
    assert targets


@pytest.mark.parametrize("edge", ["reverse", "nullable", "many"])
@pytest.mark.parametrize("operation", ["check", "exact_scope"])
def test_new_source_edge_is_witnessed_before_exclusion_grants(local, monkeypatch, edge, operation):
    if edge in {"reverse", "many"}:
        path = "posts" if edge == "reverse" else "collected_posts"
        text = SCHEMA.replace('{"path":"posts","filters":{"is_active":true}}', path)
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
            permission exclusion = viewer - folder->read
        }
        """
        source_model = Post
    install_schema(local, text)
    with sudo(reason="tests.point_source.race_fixture"):
        if edge == "nullable":
            source = Post.objects.create(title="source")
            target = Folder.objects.create(name="ban")
        else:
            source = Folder.objects.create(name="source")
            target = Post.objects.create(title="ban")
    local.write_relationships(
        [RelationshipTuple(to_object_ref(row), "viewer", ALICE) for row in (source, target)]
    )
    original = read._point_rows
    changed = []

    def stale(using, resource_type, requirements, ids):
        data = original(using, resource_type, requirements, ids)
        if resource_type == to_object_ref(source).resource_type and not changed:
            assert any(not held for row in data.values() for _key, held in row.source_matches)
            changed.append(True)
            with sudo(reason="tests.point_source.insert_edge"):
                if edge == "nullable":
                    Post.objects.using(using).filter(pk=source.pk).update(folder=target)
                elif edge == "reverse":
                    Post.objects.using(using).filter(pk=target.pk).update(folder=source)
                else:
                    target.collections.add(source)
        return data

    monkeypatch.setattr(read, "_point_rows", stale)
    if operation == "check":
        allowed = local.check_access(
            subject=ALICE, action="exclusion", resource=to_object_ref(source)
        ).allowed
    else:
        allowed = (
            source_model.objects.with_actor(ALICE)
            .with_action("exclusion")
            .filter(pk=source.pk)
            .exists()
        )
    assert not allowed
    assert changed
    assert not local.check_access(
        subject=ALICE, action="exclusion", resource=to_object_ref(source)
    ).allowed


def test_nonunique_source_identity_keeps_native_population(local, monkeypatch, metadata_reads):
    with sudo(reason="tests.point_source.nonunique_fixture"):
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
    metadata_reads.clear()
    assert local.check_access(
        subject=ALICE, action="read", resource=ObjectRef("blog/post", str(identity.pk))
    ).allowed
    own = [item for item in metadata_reads if item["type"] == "blog/post"]
    assert own and all(item["queries"] == 0 and item["data"] == {} for item in own)


@pytest.mark.parametrize("scalar_source", [False], indirect=True)
def test_unconstrained_pointer_presence_does_not_add_target_existence(
    local, scalar_source, metadata_reads
):
    install_schema(
        local,
        scalar_schema({}).replace(
            "permission read = viewer + posts->read", "permission read = authenticated"
        ),
    )
    with sudo(reason="tests.point_source.dangling_fixture"):
        row = scalar_source.objects.create(target_id=999_999, metadata=None, audience="")
    metadata_reads.clear()
    assert scalar_source.objects.with_actor(ALICE).filter(pk=row.pk).exists()
    own = [item for item in metadata_reads if item["type"] == "test/pointscalar"]
    assert own[0]["data"][str(row.pk)].source_matches == ((("test/pointscalar", "target"), True),)


def test_root_absence_fact_does_not_prune_a_nested_reference(local, metadata_reads):
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
    with sudo(reason="tests.point_source.nested_fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
    resource = to_object_ref(root)
    local.write_relationships([RelationshipTuple(resource, "viewer", ALICE)])
    metadata_reads.clear()
    assert not local.check_access(subject=ALICE, action="read", resource=resource).allowed
    facts = next(item for item in metadata_reads if item["type"] == "blog/folder")["data"][
        resource.resource_id
    ]
    assert dict(facts.source_matches) == {
        ("blog/folder", "children"): True,
        ("blog/folder", "parent"): False,
    }
    assert not Folder.objects.with_actor(ALICE).filter(pk=root.pk).exists()
    with sudo(reason="tests.point_source.remove_nested_edge"):
        child.delete()
    assert local.check_access(subject=ALICE, action="read", resource=resource).allowed
    assert Folder.objects.with_actor(ALICE).filter(pk=root.pk).exists()
