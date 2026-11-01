"""Point reads retain native to-one arrows, filters and database identity."""

from __future__ import annotations

import pytest
from django.test import override_settings

from rebac import (
    RelationshipTuple,
    SubjectRef,
    backend,
    database_context,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from tests.backend_setup import install_schema
from tests.testapp.models import CodedFolder, Folder, Post

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
