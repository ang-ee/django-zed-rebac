"""Focused SQL predicate checks before the compiler becomes the read path."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db.models import F, Value
from django.test import override_settings
from django.utils import timezone

from rebac import LocalBackend, ObjectRef, RelationshipTuple, SubjectRef, backend, sudo
from rebac._id import model_identity_fields
from rebac.backends import reset_backend
from rebac.compile import At, Bound, Compiler
from rebac.composition import ArmTag, TaggedComposition
from rebac.models.generation import SchemaGeneration
from rebac.schema import parse_zed
from tests.backend_setup import atomic_source_write, install_schema
from tests.testapp.models import AuthoredPost, Folder, VirtualFolder, VirtualPost

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pg_delta]

SCHEMA = """
definition auth/user {}
definition test/virtualfolder {
    relation owner: auth/user
    relation member: auth/user
    permission read = owner
}
definition test/virtualpost {
    relation folder: test/virtualfolder // rebac:field=folder
    relation shared: auth/user
    relation blocked: auth/user
    permission read = (shared + folder->read) - blocked
}
definition blog/post {
    relation viewer: auth/user | auth/user:* | test/virtualfolder#member
    relation denied: auth/user
    permission read = viewer - denied
}
definition blog/authoredpost {
    relation owner: auth/user // rebac:field=author
    relation viewer: auth/user:*
    permission read = owner + viewer
}
"""


@pytest.fixture(params=["denormalized", "registry"])
def setup(request, monkeypatch):
    for model in (VirtualFolder, VirtualPost):
        monkeypatch.setattr(model._meta, "rebac_id_attr", "pk")
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param):
        reset_backend()
        local = backend()
        assert isinstance(local, LocalBackend)
        schema = parse_zed(SCHEMA)
        install_schema(local, schema)
        yield local, schema
        reset_backend()


def _scope(compiler: Compiler, model, resource_type: str):
    _, field = model_identity_fields(model, "pk")
    return model._base_manager.filter(
        compiler.holds((resource_type, "read"), At(resource_type, F("pk"), field, True))
    )


def _point(compiler: Compiler, resource_type: str, object_id: str, bound=Bound.LOWER):
    condition = compiler.holds(
        (resource_type, "read"), At(resource_type, Value(object_id), None, False), bound
    )
    return SchemaGeneration.objects.filter(pk=1).filter(condition).exists()


def test_tuple_only_exclusion_and_wildcard_are_evaluated_at_identity(setup, django_user_model):
    local, schema = setup
    user = atomic_source_write(django_user_model.objects.create_user, username="compiler_actor")
    actor = SubjectRef.of("auth/user", str(user.pk))
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("blog/post", "90001"), "viewer", actor),
            RelationshipTuple(ObjectRef("blog/post", "90002"), "viewer", actor),
            RelationshipTuple(ObjectRef("blog/post", "90002"), "denied", actor),
        ]
    )
    compiler = Compiler(schema, actor, "default")
    assert _point(compiler, "blog/post", "90001")
    assert not _point(compiler, "blog/post", "90002")
    assert not _point(compiler, "blog/post", "90002", Bound.UPPER)


def test_live_fk_arrow_and_union_scope(setup, django_user_model):
    local, schema = setup
    user = atomic_source_write(django_user_model.objects.create_user, username="compiler_fk_actor")
    actor = SubjectRef.of("auth/user", str(user.pk))
    with sudo(reason="compiler test fixtures"):
        folder = VirtualFolder.objects.create(name="compiler")
        inherited = VirtualPost.objects.create(title="inherited", folder=folder)
        direct = VirtualPost.objects.create(title="direct")
        blocked = VirtualPost.objects.create(title="blocked", folder=folder)
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("test/virtualfolder", str(folder.pk)), "owner", actor),
            RelationshipTuple(ObjectRef("test/virtualpost", str(direct.pk)), "shared", actor),
            RelationshipTuple(ObjectRef("test/virtualpost", str(blocked.pk)), "blocked", actor),
        ]
    )
    compiler = Compiler(schema, actor, "default")
    assert set(_scope(compiler, VirtualPost, "test/virtualpost").values_list("pk", flat=True)) == {
        inherited.pk,
        direct.pk,
    }
    assert _point(compiler, "test/virtualpost", str(inherited.pk))
    assert not _point(compiler, "test/virtualpost", str(blocked.pk))


def test_subject_set_actor_matches_exact_tuple_and_its_members(setup):
    local, schema = setup
    actor = SubjectRef.of("auth/user", "compiler_group_member")
    group = SubjectRef.of("test/virtualfolder", "group_only_in_tuples", "member")
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("blog/post", "tuple_only_post"), "viewer", group),
            RelationshipTuple(
                ObjectRef("test/virtualfolder", "group_only_in_tuples"), "member", actor
            ),
        ]
    )
    assert _point(Compiler(schema, group, "default"), "blog/post", "tuple_only_post")
    assert _point(Compiler(schema, actor, "default"), "blog/post", "tuple_only_post")


def test_invalid_native_actor_id_does_not_break_independent_wildcard_arm(setup, django_user_model):
    local, schema = setup
    unknown = SubjectRef.of("auth/user", "not-an-integer")
    author = atomic_source_write(
        django_user_model.objects.create_user, username="compiler_real_author"
    )
    with sudo(reason="compiler invalid actor fixture"):
        post = AuthoredPost.objects.create(title="wildcard", author=author)
    local.write_relationships(
        [
            RelationshipTuple(
                ObjectRef("blog/authoredpost", str(post.pk)),
                "viewer",
                SubjectRef.of("auth/user", "*"),
            )
        ]
    )
    compiler = Compiler(schema, unknown, "default")
    assert _point(compiler, "blog/authoredpost", str(post.pk))
    assert list(_scope(compiler, AuthoredPost, "blog/authoredpost")) == [post]


def test_tagged_extension_on_deep_self_fk_compiles_within_sqlite_limit(setup):
    local, _ = setup
    schema = parse_zed("""
        definition auth/user {}
        definition blog/folder {
            relation owner: auth/user
            relation viewer: auth/user
            relation parent: blog/folder // rebac:field=parent
            permission read = (owner + parent->read) + viewer
        }
    """)
    install_schema(local, schema)
    actor = SubjectRef.of("auth/user", "deep_reader")
    folders = [
        Folder(pk=13001 + n, name=f"tagged-{n}", parent_id=13000 + n if n else None)
        for n in range(13)
    ]
    with sudo(reason="tagged recursion fixture"):
        Folder._base_manager.bulk_create(folders)
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("blog/folder", "13001"), "owner", actor),
            RelationshipTuple(
                ObjectRef("blog/folder", "13001"),
                "viewer",
                SubjectRef.of("auth/user", "extension_reader"),
            ),
        ]
    )
    permission = schema.get_permission("blog/folder", "read")
    assert permission is not None
    tagged = TaggedComposition(
        schema,
        {
            id(permission.expression.right): ArmTag(
                timezone.now() + timedelta(days=1), "extend:test"
            )
        },
        {},
    )
    compiler = Compiler(schema, actor, "default", tagged=tagged, depth_limit=16)
    assert _point(compiler, "blog/folder", str(folders[-1].pk))
    extension_actor = SubjectRef.of("auth/user", "extension_reader")
    assert _point(
        Compiler(schema, extension_actor, "default", tagged=tagged, depth_limit=16),
        "blog/folder",
        str(folders[-1].pk),
    )
    expired = TaggedComposition(
        schema,
        {
            id(permission.expression.right): ArmTag(
                timezone.now() - timedelta(days=1), "extend:test"
            )
        },
        {},
    )
    expired_compiler = Compiler(schema, extension_actor, "default", tagged=expired, depth_limit=16)
    assert not _point(
        expired_compiler,
        "blog/folder",
        "13001",
    )
    assert not _point(
        Compiler(schema, extension_actor, "default", tagged=expired, depth_limit=16),
        "blog/folder",
        str(folders[-1].pk),
    )
