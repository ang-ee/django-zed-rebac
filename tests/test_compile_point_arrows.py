"""Point reads retain native to-one arrows, filters and database identity."""

from __future__ import annotations

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    backend,
    database_context,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from tests.backend_setup import install_schema
from tests.testapp.models import Attachment, CodedFolder, Folder, Post

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pg_delta]

ALICE = SubjectRef.of("auth/user", "point-arrow-alice")
BOB = SubjectRef.of("auth/user", "point-arrow-bob")

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    relation parent: blog/folder // rebac:field=parent
    permission read = viewer + parent->read
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
definition blog/codedfolder {
    relation viewer: auth/user
    relation parent: blog/codedfolder // rebac:field=parent
    permission read = viewer + parent->read
}
"""


@pytest.fixture(params=["denormalized", "registry"])
def storage(request):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        yield backend()
        reset_backend()


def test_point_arrow_preserves_null_missing_and_actor_outcomes(storage):
    install_schema(storage, SCHEMA)
    with sudo(reason="tests.point_arrow.fixture"):
        parent = Folder.objects.create(name="shared")
        child = Folder.objects.create(name="inherited", parent=parent)
        denied = Folder.objects.create(name="unshared")
        inherited = Post.objects.create(title="inherited", folder=child)
        hidden = Post.objects.create(title="hidden", folder=denied)
        empty = Post.objects.create(title="empty")
    storage.write_relationships([RelationshipTuple(to_object_ref(parent), "viewer", ALICE)])

    assert Post.objects.with_actor(ALICE).filter(pk=inherited.pk).exists()
    assert not Post.objects.with_actor(BOB).filter(pk=inherited.pk).exists()
    assert not Post.objects.with_actor(ALICE).filter(pk=hidden.pk).exists()
    assert not Post.objects.with_actor(ALICE).filter(pk=empty.pk).exists()
    assert not Post.objects.with_actor(ALICE).filter(pk=999_999).exists()
    assert set(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == {inherited.pk}

    with sudo(reason="tests.point_arrow.remove_target"):
        child.delete()
    assert not Post.objects.with_actor(ALICE).filter(pk=inherited.pk).exists()


def test_point_arrow_keeps_source_filters_and_original_queryset_predicates(storage):
    filtered = SCHEMA.replace(
        "relation folder: blog/folder // rebac:field=folder",
        'relation folder: blog/folder // rebac:field={"path":"folder",'
        '"filters":{"title":"included"}}',
    )
    install_schema(storage, filtered)
    with sudo(reason="tests.point_arrow.filtered_fixture"):
        folder = Folder.objects.create(name="shared")
        included = Post.objects.create(title="included", folder=folder)
        excluded = Post.objects.create(title="excluded", folder=folder)
        empty = Post.objects.create(title="included")
    storage.write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", ALICE)])

    assert Post.objects.with_actor(ALICE).filter(pk=included.pk).exists()
    assert not Post.objects.with_actor(ALICE).filter(pk=excluded.pk).exists()
    assert not Post.objects.with_actor(ALICE).filter(pk=empty.pk).exists()
    assert not Post.objects.with_actor(ALICE).filter(pk=included.pk, title="excluded").exists()
    assert not Post.objects.with_actor(BOB).filter(pk=included.pk).exists()
    assert set(Post.objects.with_actor(ALICE).values_list("pk", flat=True)) == {included.pk}


def test_point_arrow_maps_to_field_instead_of_treating_its_value_as_target_pk(storage):
    install_schema(storage, SCHEMA)
    with sudo(reason="tests.point_arrow.alternate_key_fixture"):
        parent = CodedFolder.objects.create(pk=71, code="900")
        child = CodedFolder.objects.create(pk=72, code="child", parent=parent)
        other = CodedFolder.objects.create(pk=900, code="other")
        empty = CodedFolder.objects.create(pk=73, code=None)
    storage.write_relationships(
        [
            RelationshipTuple(to_object_ref(parent), "viewer", ALICE),
            RelationshipTuple(to_object_ref(other), "viewer", BOB),
        ]
    )

    assert child.parent_id == "900"
    assert CodedFolder.objects.with_actor(ALICE).filter(pk=child.pk).exists()
    assert not CodedFolder.objects.with_actor(BOB).filter(pk=child.pk).exists()
    assert not CodedFolder.objects.with_actor(ALICE).filter(pk=empty.pk).exists()
    assert set(CodedFolder.objects.with_actor(ALICE).values_list("pk", flat=True)) == {
        parent.pk,
        child.pk,
    }


@pytest.mark.django_db(transaction=True, databases={"default", "selected"})
def test_point_arrow_uses_selected_database_with_colliding_source_and_target_ids(storage):
    for alias in ("default", "selected"):
        with database_context(alias):
            install_schema(storage, SCHEMA, using=alias)
            with sudo(reason="tests.point_arrow.database_fixture"):
                folder = Folder.objects.using(alias).create(pk=81, name=alias)
                Post.objects.using(alias).create(pk=71, title=alias, folder=folder)
            storage.write_relationships(
                [
                    RelationshipTuple(
                        to_object_ref(folder), "viewer", ALICE if alias == "selected" else BOB
                    ),
                ]
            )

    assert Post.objects.using("selected").with_actor(ALICE).filter(pk=71).exists()
    assert not Post.objects.using("default").with_actor(ALICE).filter(pk=71).exists()
    assert Post.objects.using("default").with_actor(BOB).filter(pk=71).exists()
    assert not Post.objects.using("selected").with_actor(BOB).filter(pk=71).exists()


GENERIC_SCHEMA = (
    SCHEMA
    + """
definition test/attachment {
    relation folder: blog/folder // rebac:field=target
    relation post: blog/post // rebac:field=target
    relation viewer: auth/user
    permission read = folder->read + post->read
    permission outside = viewer - (folder->read + post->read)
}
"""
)


def test_generic_point_arrow_keeps_type_target_presence_and_actor_scope(storage):
    install_schema(storage, GENERIC_SCHEMA)
    folder_type = ContentType.objects.get_for_model(Folder)
    post_type = ContentType.objects.get_for_model(Post)
    with sudo(reason="tests.generic_point.fixture"):
        folder = Folder.objects.create(pk=71, name="shared")
        Post.objects.create(pk=71, title="different type")
        allowed = Attachment.objects.create(content_type=folder_type, object_id=71)
        wrong = Attachment.objects.create(content_type=post_type, object_id=71)
        dangling = Attachment.objects.create(content_type=folder_type, object_id=900)
    storage.write_relationships(
        [
            RelationshipTuple(to_object_ref(folder), "viewer", ALICE),
            RelationshipTuple(ObjectRef("blog/folder", "900"), "viewer", ALICE),
        ]
    )
    query = Attachment.objects.with_actor(ALICE).filter(pk=allowed.pk)
    assert query.exists()
    assert not query.all().filter(label="not stored").exists()
    assert not Attachment.objects.with_actor(BOB).filter(pk=allowed.pk).exists()
    assert not Attachment.objects.with_actor(ALICE).filter(pk=wrong.pk).exists()
    assert not Attachment.objects.with_actor(ALICE).filter(pk=dangling.pk).exists()
    assert not Attachment.objects.with_actor(ALICE).filter(pk=999_999).exists()
    for edge, expected in ((allowed, True), (wrong, False), (dangling, False)):
        assert (
            storage.check_access(subject=ALICE, action="read", resource=to_object_ref(edge)).allowed
            is expected
        )
    assert set(Attachment.objects.with_actor(ALICE).values_list("pk", flat=True)) == {allowed.pk}

    # A tuple grant outlives the target row; a GFK still must name a stored row.
    with sudo(reason="tests.generic_point.remove_target"):
        folder.delete()
    assert not query.all().exists()
    assert not storage.check_access(
        subject=ALICE, action="read", resource=to_object_ref(allowed)
    ).allowed


def test_generic_point_arrow_keeps_filters_and_exclusion_null_semantics(storage):
    filtered = GENERIC_SCHEMA.replace(
        "relation folder: blog/folder // rebac:field=target",
        'relation folder: blog/folder // rebac:field={"path":"target","filters":{"label":"included"}}',
    )
    install_schema(storage, filtered)
    folder_type = ContentType.objects.get_for_model(Folder)
    with sudo(reason="tests.generic_point.filtered_fixture"):
        folder = Folder.objects.create(name="shared")
        included = Attachment.objects.create(
            content_type=folder_type, object_id=folder.pk, label="included"
        )
        excluded = Attachment.objects.create(
            content_type=folder_type, object_id=folder.pk, label="excluded"
        )
    storage.write_relationships(
        [
            RelationshipTuple(to_object_ref(folder), "viewer", ALICE),
            RelationshipTuple(to_object_ref(included), "viewer", ALICE),
            RelationshipTuple(to_object_ref(excluded), "viewer", ALICE),
        ]
    )
    for edge, readable, outside in ((included, True, False), (excluded, False, True)):
        ref = to_object_ref(edge)
        assert storage.check_access(subject=ALICE, action="read", resource=ref).allowed is readable
        assert (
            storage.check_access(subject=ALICE, action="outside", resource=ref).allowed is outside
        )
        assert Attachment.objects.with_actor(ALICE).filter(pk=edge.pk).exists() is readable
        assert (
            Attachment.objects.with_actor(ALICE).with_action("outside").filter(pk=edge.pk).exists()
            is outside
        )
    with sudo(reason="tests.generic_point.remove_excluded_target"):
        folder.delete()
    assert (
        Attachment.objects.with_actor(ALICE).with_action("outside").filter(pk=included.pk).exists()
    )
    assert storage.check_access(
        subject=ALICE, action="outside", resource=to_object_ref(included)
    ).allowed


@pytest.mark.django_db(transaction=True, databases={"default", "selected"})
def test_generic_point_arrow_uses_the_selected_database_for_source_and_target(storage):
    for alias in ("default", "selected"):
        with database_context(alias):
            install_schema(storage, GENERIC_SCHEMA, using=alias)
            content_type = ContentType.objects.db_manager(alias).get_for_model(Folder)
            with sudo(reason="tests.generic_point.selected_fixture"):
                folder = Folder.objects.using(alias).create(pk=71, name=alias)
                Attachment.objects.using(alias).create(
                    pk=81, content_type=content_type, object_id=71
                )
            storage.write_relationships(
                [
                    RelationshipTuple(
                        to_object_ref(folder), "viewer", ALICE if alias == "selected" else BOB
                    ),
                ]
            )
    selected = Attachment.objects.using("selected").with_actor(ALICE).filter(pk=81)
    with CaptureQueriesContext(connection) as default_reads:
        assert selected.exists()
    assert not default_reads.captured_queries
    assert not Attachment.objects.with_actor(ALICE).filter(pk=81).exists()
    assert Attachment.objects.with_actor(BOB).filter(pk=81).exists()
    assert not selected.all().with_actor(BOB).exists()


@pytest.mark.parametrize("operation", ["check", "scope"])
def test_generic_point_arrow_keeps_stale_type_witness_in_an_exclusion(
    storage, monkeypatch, operation
):
    from rebac.compile import read

    install_schema(storage, GENERIC_SCHEMA)
    folder_type = ContentType.objects.get_for_model(Folder)
    post_type = ContentType.objects.get_for_model(Post)
    with sudo(reason="tests.generic_point.stale_type_fixture"):
        folder = Folder.objects.create(pk=71, name="shared")
        Post.objects.create(pk=71, title="unshared")
        edge = Attachment.objects.create(content_type=post_type, object_id=71)
    storage.write_relationships(
        [
            RelationshipTuple(to_object_ref(folder), "viewer", ALICE),
            RelationshipTuple(to_object_ref(edge), "viewer", ALICE),
        ]
    )
    ref = to_object_ref(edge)
    assert storage.check_access(subject=ALICE, action="outside", resource=ref).allowed
    # Same native race harness as the GFK tests: preparation read the old CT.
    stale = {ref.resource_id: read.PointData(types=(("content_type_id", folder_type.pk),))}
    monkeypatch.setattr(read, "_point_rows", lambda *_: stale)
    if operation == "check":
        assert not storage.check_access(subject=ALICE, action="outside", resource=ref).allowed
    else:
        assert (
            not Attachment.objects.with_actor(ALICE)
            .with_action("outside")
            .filter(pk=edge.pk)
            .exists()
        )
