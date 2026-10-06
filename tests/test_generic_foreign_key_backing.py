"""Relations backed by a GenericForeignKey: an edge's permissions follow the row it names."""

from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext, isolate_apps

from rebac import (
    NoActorResolvedError,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SchemaError,
    SubjectRef,
    actor_context,
    check_permission,
    generic_target,
    sudo,
    to_object_ref,
)
from rebac.compile import read
from rebac.field_backing import field_backing_model_errors, resolve_field_backing
from rebac.schema import parse_zed
from rebac.testing import install_schema
from tests.reference_harness import assert_reads_match, assert_subjects_match
from tests.testapp.models import (
    Attachment,
    BackingStage,
    Folder,
    NativeParentLinkedChild,
    NativeParentLinkedResource,
    Post,
    SluggedPost,
)

pytestmark = pytest.mark.django_db

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    relation viewer: auth/user
    permission read = (owner + viewer)
    permission write = owner
}
definition blog/post {
    relation owner: auth/user
    permission read = owner
    permission write = owner
}
definition test/nativeparentlinkedresource {
    relation owner: auth/user
    permission read = owner
    permission write = owner
}
definition test/attachment {
    relation folder:   blog/folder                    // rebac:field=target
    relation post:     blog/post                      // rebac:field=target
    relation resource: test/nativeparentlinkedresource // rebac:field=target
    permission create = ((folder->write + post->write) + resource->write)
    permission write  = ((folder->write + post->write) + resource->write)
    permission delete = ((folder->write + post->write) + resource->write)
    permission read   = ((folder->read + post->read) + resource->read)
}
"""
ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")


def at(row):
    """The filter keywords that store an edge to ``row``."""
    return generic_target(row).lookups(Attachment, "target")


def edge(row=None, **columns):
    with sudo(reason="test.fixture"):
        return Attachment.objects.create(**(at(row) if row is not None else columns))


@pytest.fixture
def world():
    local = install_schema(SCHEMA)
    owner = get_user_model().objects.create(username="someone")
    with sudo(reason="test.fixture"):
        rows = SimpleNamespace(
            mine=Folder.objects.create(name="mine"),
            shared=Folder.objects.create(name="shared"),
            other=Folder.objects.create(name="other"),
            post=Post.objects.create(title="mine"),
            child=NativeParentLinkedChild.objects.create(name="child", owner=owner),
            stage=BackingStage.objects.create(),
        )
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(rows.mine), "owner", ALICE),
            RelationshipTuple(to_object_ref(rows.shared), "viewer", ALICE),
            RelationshipTuple(to_object_ref(rows.post), "owner", ALICE),
            RelationshipTuple(
                ObjectRef("test/nativeparentlinkedresource", str(rows.child.pk)), "owner", ALICE
            ),
        ]
    )
    rows.local = local
    return rows


def relation(schema_text, name, type_="test/attachment"):
    schema = parse_zed(schema_text)
    definition = schema.get_definition(type_)
    return definition, next(r for r in definition.relations if r.name == name)


# ---------- Declaration ----------


def test_one_generic_foreign_key_backs_a_relation_per_type():
    for name, model in (("folder", Folder), ("post", Post)):
        resolved = resolve_field_backing(*relation(SCHEMA, name))
        assert (resolved.generic.name, resolved.generic.ct_field, resolved.generic.fk_field) == (
            "target",
            "content_type",
            "object_id",
        )
        assert resolved.target_model is model
        assert resolved.target_values_path() == "object_id"
        # Nothing constrains the object id: keys are read through the target's rows.
        assert not resolved.keeps_target("default")


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ("blog/folder | blog/post", "exactly one subject type"),
        ("blog/folder#viewer", "subject relation"),
        ("blog/folder:*", "wildcard"),
        # A child of a typed parent: its edges are stored under the parent.
        ("test/nativeparentlinkedchild", "canonical"),
        ("blog/sluggedpost", "primary key"),
        ("blog/textidentityprimarypost", "object id"),
    ],
)
def test_a_generic_backing_that_could_never_match_is_refused(declaration, message):
    text = f"""
    definition auth/user {{}}
    definition blog/folder {{
        relation viewer: auth/user
    }}
    definition test/attachment {{
        relation target_row: {declaration} // rebac:field=target
    }}
    """
    errors = field_backing_model_errors(*relation(text, "target_row"))
    assert any(message in error for error in errors), errors


# ---------- The canonical target ----------


def test_the_canonical_target_of_a_row(world):
    folder = generic_target(world.mine)
    assert folder.content_type == ContentType.objects.get_for_model(Folder)
    assert (folder.object_id, folder.ref) == (world.mine.pk, to_object_ref(world.mine))
    assert folder.lookups(Attachment, "target") == {
        "content_type": folder.content_type,
        "object_id": world.mine.pk,
    }
    # A multi-table child is stored under its topmost typed ancestor.
    child = generic_target(world.child)
    assert child.content_type == ContentType.objects.get_for_model(NativeParentLinkedResource)
    assert child.ref == ObjectRef("test/nativeparentlinkedresource", str(world.child.pk))
    with pytest.raises(ValueError, match="resource type"):
        generic_target(world.stage)
    with pytest.raises(ValueError, match="GenericForeignKey"):
        folder.lookups(Attachment, "label")


@isolate_apps("tests.testapp")
def test_a_proxy_is_stored_as_its_concrete_row(world):
    class FolderView(Folder):
        class Meta:
            app_label = "testapp"
            proxy = True

    view = FolderView(pk=world.mine.pk, name="mine")
    assert generic_target(view) == generic_target(world.mine)


# ---------- Writes ----------


def test_creating_an_edge_needs_write_on_its_target(world):
    with actor_context(ALICE):
        for row in (world.mine, world.post, world.child):
            assert Attachment.objects.create(**at(row)).pk
        for row in (world.shared, world.other):
            with pytest.raises(PermissionDenied):
                Attachment.objects.create(**at(row))


def test_an_edge_no_relation_admits_is_refused(world):
    local = world.local
    with sudo(reason="test.fixture"):
        gone = Folder.objects.create(name="gone")
        gone_pk = gone.pk
        gone.delete()
    # A grant on the id of a row that is gone.
    local.write_relationships(
        [RelationshipTuple(ObjectRef("blog/folder", str(gone_pk)), "owner", ALICE)]
    )
    content_type = ContentType.objects.get_for_model
    refused = [
        # A model with no resource type.
        {"content_type": content_type(BackingStage), "object_id": world.stage.pk},
        # A typed model that no relation of the edge names.
        {"content_type": content_type(SluggedPost), "object_id": 1},
        # A child stored under its own content type, not its typed ancestor's.
        {"content_type": content_type(NativeParentLinkedChild), "object_id": world.child.pk},
        # A target row that is gone.
        {"content_type": content_type(Folder), "object_id": gone_pk},
    ]
    with actor_context(ALICE):
        for columns in refused:
            with pytest.raises(PermissionDenied):
                Attachment.objects.create(**columns)
    with sudo(reason="test.fixture"):
        assert not Attachment.objects.exists()


def test_bulk_create_gates_every_edge(world):
    with actor_context(ALICE):
        with pytest.raises(PermissionDenied):
            Attachment.objects.bulk_create(
                [Attachment(**at(world.mine)), Attachment(**at(world.other))]
            )
        made = Attachment.objects.bulk_create(
            [Attachment(**at(world.mine)), Attachment(**at(world.post))]
        )
    assert len(made) == 2
    with sudo(reason="test.fixture"):
        assert Attachment.objects.count() == 2


def test_deleting_an_edge_needs_delete_on_it(world):
    mine, shared, other = edge(world.mine), edge(world.shared), edge(world.other)
    with actor_context(ALICE):
        with pytest.raises(PermissionDenied):
            other.delete()
        rows = Attachment.objects.with_actor(ALICE)
        # Readable, not deletable.
        with pytest.raises(PermissionDenied):
            rows.filter(pk=shared.pk).delete()
        assert rows.filter(pk=mine.pk).delete()[0] == 1
    with sudo(reason="test.verify"):
        assert set(Attachment.objects.values_list("pk", flat=True)) == {shared.pk, other.pk}


def test_an_edge_is_not_moved_under_an_actor(world):
    moving = edge(world.mine)
    post = generic_target(world.post)
    with sudo(reason="test.fixture"):
        also_mine = Folder.objects.create(name="also mine")
    world.local.write_relationships([RelationshipTuple(to_object_ref(also_mine), "owner", ALICE)])
    with actor_context(ALICE):
        rows = Attachment.objects.with_actor(ALICE)
        loaded = rows.get(pk=moving.pk)
        loaded.label = "renamed"
        loaded.save()
        assert rows.filter(pk=moving.pk).update(label="again") == 1
        loaded.content_type, loaded.object_id = post.content_type, post.object_id
        with pytest.raises(PermissionDenied, match="Delete the edge"):
            loaded.save()
        for columns in ({"object_id": also_mine.pk}, {"content_type": post.content_type}):
            with pytest.raises(PermissionDenied, match="Delete the edge"):
                rows.filter(pk=moving.pk).update(**columns)
    with sudo(reason="test.move"):
        assert Attachment.objects.filter(pk=moving.pk).update(object_id=also_mine.pk) == 1


def test_a_tuple_cannot_be_written_to_a_generic_relation(world):
    with pytest.raises(SchemaError, match=r"Attachment\.target"):
        world.local.write_relationships(
            [
                RelationshipTuple(
                    ObjectRef("test/attachment", "1"), "folder", SubjectRef.of("blog/folder", "1")
                )
            ]
        )


# ---------- Reads ----------


@pytest.fixture
def edges(world):
    with sudo(reason="test.fixture"):
        gone = Folder.objects.create(name="gone")
        gone_pk = gone.pk
        gone.delete()
    world.local.write_relationships(
        [RelationshipTuple(ObjectRef("blog/folder", str(gone_pk)), "owner", ALICE)]
    )
    content_type = ContentType.objects.get_for_model
    made = {name: edge(getattr(world, name)) for name in ("mine", "shared", "other", "post")}
    made["child"] = edge(world.child)
    made["untyped"] = edge(content_type=content_type(BackingStage), object_id=world.stage.pk)
    made["gone"] = edge(content_type=content_type(Folder), object_id=gone_pk)
    return made


READABLE = ("mine", "shared", "post", "child")


@pytest.mark.parametrize("decided", [True, False], ids=["decided", "inline"])
def test_a_scope_reads_the_edges_whose_target_is_readable(world, edges, monkeypatch, decided):
    if not decided:
        monkeypatch.setattr(read, "_ROW_LIMIT", 0)
    rows = Attachment.objects.with_actor(ALICE)
    assert set(rows.values_list("pk", flat=True)) == {edges[name].pk for name in READABLE}
    assert not Attachment.objects.with_actor(BOB).exists()
    # The edges of one record, with no check per edge.
    with CaptureQueriesContext(connection) as queries:
        assert list(rows.filter(**at(world.mine))) == [edges["mine"]]
    # One decision for each target type, then the edges.
    assert len(queries) == 3 + 1


def test_checks_enumeration_and_subjects_agree_with_the_reference(world, edges):
    local = world.local
    for name, row in edges.items():
        resource = ObjectRef("test/attachment", str(row.pk))
        allowed = local.check_access(subject=ALICE, action="read", resource=resource).allowed
        assert allowed is (name in READABLE), name
    accessible = local.accessible(subject=ALICE, action="read", resource_type="test/attachment")
    assert set(accessible) == {str(edges[name].pk) for name in READABLE}
    resources = [ObjectRef("test/attachment", str(row.pk)) for row in edges.values()]
    assert_reads_match(
        subjects=[ALICE, BOB],
        resources=resources,
        actions=["read", "write", "create", "delete"],
    )
    assert_subjects_match(
        resources=resources, actions=["read", "write"], subject_types=["auth/user"]
    )


FILTERED = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    permission read = owner
    permission write = owner
}
definition test/attachment {
    relation pinned: blog/folder // rebac:field={"path":"target","filters":{"label":"pinned"}}
    permission create = pinned->write
    permission read = pinned->read
}
"""


def test_a_generic_backing_filters_on_the_edge_columns(world):
    local = install_schema(FILTERED)
    local.write_relationships([RelationshipTuple(to_object_ref(world.mine), "owner", ALICE)])
    with actor_context(ALICE):
        pinned = Attachment.objects.create(label="pinned", **at(world.mine))
        with pytest.raises(PermissionDenied):
            Attachment.objects.create(label="loose", **at(world.mine))
    loose = edge(world.mine)
    rows = Attachment.objects.with_actor(ALICE)
    assert set(rows.values_list("pk", flat=True)) == {pinned.pk}
    assert loose.pk not in set(rows.values_list("pk", flat=True))


def test_the_write_gates_see_both_columns_of_the_reference(world):
    from rebac.watch import gate_policy

    policy = gate_policy("default")
    columns = {"content_type", "content_type_id", "object_id"}
    assert policy.edges["testapp.attachment"] == frozenset(columns)
    assert columns <= set(policy.watched["testapp.attachment"].fields)


# ---------- A check as the effective actor ----------


def test_check_permission_answers_as_the_effective_actor(world):
    mine = to_object_ref(world.mine)
    assert check_permission("write", mine, actor=ALICE).allowed
    assert not check_permission("write", world.other, actor=ALICE).allowed
    with actor_context(ALICE):
        assert check_permission("write", mine).allowed
        assert not check_permission("write", world.other).allowed
    with sudo(reason="test.check"):
        assert check_permission("write", world.other).allowed
        # An explicit actor is asked even under sudo.
        assert not check_permission("write", world.other, actor=ALICE).allowed
    with pytest.raises(NoActorResolvedError):
        check_permission("write", mine)
