"""Proposed-row create checks honour resource-independent grants.

``LocalBackend.check_access`` with an empty ``resource_id`` answers the
legacy row-independent part of that evaluation. ``check_new`` is the create
gate and overlays the candidate's forward relations. A permission built from
terms that don't depend on a concrete row — the built-in ``authenticated`` /
``anonymous`` actors, or a const-backed arrow that resolves to a fixed object
regardless of row id — must grant even though no accessible row exists yet.
Relation-based terms (``owner``) still resolve through the ``accessible()``
fallback; they evaluate ``False`` against the empty id and so never spuriously
grant via the row-independent path.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings

from rebac import (
    LocalBackend,
    MissingActorError,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    backend,
    sudo,
)
from rebac.actors import anonymous_actor
from rebac.backends import reset_backend
from rebac.schema import parse_zed


class CreateWriteRouter:
    def db_for_read(self, model, **hints):
        del hints
        if model._meta.app_label == "rebac":
            return "default"
        return "missing-read-replica"

    def db_for_write(self, model, **hints):
        del model, hints
        return "default"


UNIT_SCHEMA = """
definition auth/user {}

definition auth/role {
    relation member: auth/user
}

definition blog/post {
    relation owner: auth/user
    relation admin: auth/role // rebac:const=superadmin
    permission create_authed = authenticated
    permission create_anon   = anonymous
    permission create_owned  = owner
    permission create_admin  = admin->member
}
"""


@pytest.fixture
def be(db):
    b = LocalBackend()
    b.set_schema(parse_zed(UNIT_SCHEMA))
    return b


def _user(id_: str) -> SubjectRef:
    return SubjectRef.of("auth/user", id_)


def _new_post() -> ObjectRef:
    # Empty resource_id => "a not-yet-persisted row of this type".
    return ObjectRef("blog/post", "")


def _check(be: LocalBackend, *, subject: SubjectRef, action: str) -> bool:
    return be.has_access(subject=subject, action=action, resource=_new_post())


# ---------- built-in actor terms ----------


def test_create_authenticated_grants_authenticated_subject(be) -> None:
    assert _check(be, subject=_user("1"), action="create_authed") is True


def test_create_authenticated_denies_anonymous(be) -> None:
    assert _check(be, subject=anonymous_actor(), action="create_authed") is False


def test_create_anonymous_grants_anonymous_subject(be) -> None:
    assert _check(be, subject=anonymous_actor(), action="create_anon") is True


# ---------- relation-based term still routes through the accessible() fallback ----------


def test_create_owner_denies_without_an_owned_row(be) -> None:
    # No row owned anywhere => the empty-id eval is False AND accessible() is empty.
    assert _check(be, subject=_user("1"), action="create_owned") is False


def test_create_owner_grants_via_accessible_fallback(be) -> None:
    # Owning any row makes accessible(create_owned) non-empty -> the fallback grants.
    be.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/post", "p1"),
                relation="owner",
                subject=_user("1"),
            )
        ]
    )
    assert _check(be, subject=_user("1"), action="create_owned") is True


# ---------- const-backed arrow (universal-admin style) ----------


def test_create_const_admin_grants_member_of_const_role(be) -> None:
    # ``admin`` is const-bound to auth/role:superadmin for every blog/post row, so
    # the arrow resolves regardless of the (empty) resource id.
    be.write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("auth/role", "superadmin"),
                relation="member",
                subject=_user("1"),
            )
        ]
    )
    assert _check(be, subject=_user("1"), action="create_admin") is True


def test_create_const_admin_denies_non_member(be) -> None:
    assert _check(be, subject=_user("9"), action="create_admin") is False


# ---------- end-to-end: the pre_save create signal gate ----------

INTEGRATION_SCHEMA = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    permission read   = owner
    permission write  = owner
    permission create = authenticated
}
"""


@pytest.fixture
def _global_backend(db):
    reset_backend()
    backend().set_schema(parse_zed(INTEGRATION_SCHEMA))
    yield
    reset_backend()


def _django_user(username: str):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username=username, is_active=True)


@pytest.mark.django_db
def test_authenticated_actor_can_create_through_pre_save_gate(_global_backend) -> None:
    from tests.testapp.models import Post

    alice = _django_user("alice")
    # The candidate carries no relation, but create = authenticated grants.
    with actor_context(SubjectRef.of("auth/user", str(alice.pk))):
        post = Post.objects.create(title="hello")
    assert post.pk is not None


@pytest.mark.django_db
def test_anonymous_actor_cannot_create_when_create_is_authenticated(_global_backend) -> None:
    from tests.testapp.models import Post

    with actor_context(anonymous_actor()):
        with pytest.raises(PermissionDenied):
            Post.objects.create(title="nope")


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["create", "insert"])
def test_queryset_create_pins_actor_without_ambient_context(_global_backend, operation) -> None:
    from tests.testapp.models import Post

    actor = SubjectRef.of("auth/user", "alice")
    queryset = Post.objects.with_actor(actor)
    candidate = Post(title="hello").with_actor(_user("original"))
    post = queryset.create(title="hello") if operation == "create" else queryset.insert(candidate)

    assert post.pk is not None
    assert post.actor() == actor
    if operation == "insert":
        assert post is candidate


@pytest.mark.django_db
@pytest.mark.parametrize("ambient_bypass", [False, True])
@pytest.mark.parametrize("operation", ["create", "insert"])
def test_queryset_create_explicit_denied_actor_beats_ambient_scope(
    _global_backend, ambient_bypass, operation
) -> None:
    from tests.testapp.models import Post

    context = (
        sudo(reason="test.ambient")
        if ambient_bypass
        else actor_context(SubjectRef.of("auth/user", "alice"))
    )
    with context:
        queryset = Post.objects.with_actor(anonymous_actor())
        with pytest.raises(PermissionDenied):
            if operation == "create":
                queryset.create(title="forbidden")
            else:
                queryset.insert(Post(title="forbidden").sudo(reason="test.prepared"))
    assert not Post.objects.sudo(reason="test.verify").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["create", "insert"])
def test_explicit_queryset_sudo_allows_create_without_leaving_instance_elevated(
    _global_backend, operation
) -> None:
    from tests.testapp.models import Post

    queryset = Post.objects.sudo(reason="test.create")
    post = (
        queryset.create(title="fixture")
        if operation == "create"
        else queryset.insert(Post(title="fixture"))
    )

    assert post.pk is not None
    assert not post.is_sudo()


@pytest.mark.django_db
def test_queryset_insert_clears_sudo_after_save_failure(_global_backend) -> None:
    from django.db import IntegrityError, transaction

    from tests.testapp.models import Post

    stored = Post.objects.sudo(reason="test.fixture").create(title="stored")
    candidate = Post(pk=stored.pk, title="duplicate")
    with pytest.raises(IntegrityError), transaction.atomic():
        Post.objects.sudo(reason="test.insert").insert(candidate)

    assert not candidate.is_sudo()
    assert candidate._state.adding
    assert Post.objects.sudo(reason="test.verify").get(pk=stored.pk).title == "stored"


@pytest.mark.django_db
def test_queryset_insert_rejects_saved_instance(_global_backend) -> None:
    from tests.testapp.models import Post

    stored = Post.objects.sudo(reason="test.fixture").create(title="stored")
    with pytest.raises(ValueError, match="unsaved"):
        Post.objects.insert(stored)

    assert Post.objects.sudo(reason="test.verify").count() == 1


@pytest.mark.parametrize(
    ("queryset_model", "candidate_model"),
    [("Post", "Folder"), ("Post", "VirtualPost"), ("VirtualPost", "Post")],
)
def test_queryset_insert_rejects_wrong_and_proxy_models(queryset_model, candidate_model) -> None:
    from tests.testapp import models

    model = getattr(models, queryset_model)
    candidate = getattr(models, candidate_model)()
    with pytest.raises(TypeError, match=f"exact {queryset_model} instances"):
        model.objects.insert(candidate)
    assert candidate._state.adding


def test_queryset_insert_rejects_conflicting_database_alias() -> None:
    from tests.testapp.models import Post

    candidate = Post(title="wrong database")
    candidate._state.db = "other"
    with pytest.raises(ValueError, match=r"other.*default"):
        Post.objects.insert(candidate)
    assert candidate._state.db == "other"
    assert candidate._state.adding


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["create", "insert"])
@pytest.mark.parametrize("allowed", [False, True])
def test_queryset_insert_factory_runs_once_and_keeps_companion_write_atomic(
    _global_backend, monkeypatch: pytest.MonkeyPatch, operation, allowed
) -> None:
    from django.db import transaction

    from rebac.managers import RebacManager, RebacQuerySet
    from tests.testapp.models import Folder, Post

    calls = []

    class FactoryQuerySet(RebacQuerySet):
        def insert(self, obj):
            calls.append(obj)
            with transaction.atomic(using=self.db):
                obj.folder = (
                    Folder.objects.sudo(reason="test.factory")
                    .using(self.db)
                    .create(name="companion")
                )
                return super().insert(obj)

    manager = RebacManager.from_queryset(FactoryQuerySet)()
    manager.model = Post
    monkeypatch.setattr(Post, "objects", manager)
    candidate = Post(title="factory")

    def persist():
        if operation == "create":
            return Post.objects.create(title=candidate.title)
        return Post.objects.insert(candidate)

    with actor_context(_user("allowed") if allowed else anonymous_actor()):
        if allowed:
            result = persist()
            assert calls == [result]
            assert result.folder.name == "companion"
            if operation == "insert":
                assert result is candidate
        else:
            with pytest.raises(PermissionDenied):
                persist()
            assert len(calls) == 1

    assert Post.objects.sudo(reason="test.verify").count() == int(allowed)
    assert Folder.objects.sudo(reason="test.verify").count() == int(allowed)


@pytest.mark.django_db
@pytest.mark.parametrize("bound_to_alias", [False, True])
def test_manager_insert_uses_db_manager_alias(
    _global_backend, django_db_blocker, tmp_path, bound_to_alias
) -> None:
    from django.db import connection, connections

    from tests.testapp.models import Folder, Post

    alias = "insert_target"
    target = connection.copy(alias=alias)
    target.settings_dict["NAME"] = str(tmp_path / "insert.sqlite3")
    connections[alias] = target
    try:
        with django_db_blocker.unblock():
            with target.schema_editor() as editor:
                editor.create_model(Folder)
                editor.create_model(Post)
            candidate = Post(title="other database")
            if bound_to_alias:
                candidate._state.db = alias
            with actor_context(_user("alice")):
                result = Post.objects.db_manager(alias).insert(candidate)
            assert result is candidate
            assert result._state.db == alias
            assert Post._base_manager.using(alias).get(pk=result.pk).title == "other database"
    finally:
        target.close()
        del connections[alias]

    assert not Post.objects.sudo(reason="test.verify").exists()


@pytest.mark.django_db
def test_bulk_create_requires_actor(_global_backend) -> None:
    from tests.testapp.models import Post

    with pytest.raises(MissingActorError):
        Post.objects.bulk_create([Post(title="forbidden")])
    assert not Post.objects.sudo(reason="test.verify").exists()


@pytest.mark.django_db
def test_bulk_create_rejects_denied_actor_and_pins_authorized_actor(_global_backend) -> None:
    from tests.testapp.models import Post

    with sudo(reason="test.ambient"):
        with pytest.raises(PermissionDenied):
            Post.objects.with_actor(anonymous_actor()).bulk_create([Post(title="forbidden")])
    actor = SubjectRef.of("auth/user", "alice")
    rows = Post.objects.with_actor(actor).bulk_create([Post(title="allowed")])
    assert rows[0].pk is not None
    assert rows[0].actor() == actor
    assert Post.objects.sudo(reason="test.verify").count() == 1


@pytest.mark.django_db
def test_bulk_create_cannot_update_conflicts_with_only_create_permission(_global_backend) -> None:
    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="private")
    actor = SubjectRef.of("auth/user", "alice")

    with pytest.raises(PermissionDenied):
        Post.objects.with_actor(actor).bulk_create(
            [Post(pk=post.pk, title="overwritten")],
            update_conflicts=True,
            update_fields=["title"],
            unique_fields=["pk"],
        )

    assert Post.objects.sudo(reason="test.verify").get(pk=post.pk).title == "private"


PARENT_CREATE_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    permission write = owner
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission create = folder->write
}
"""


@pytest.fixture
def parent_create_backend(db):
    reset_backend()
    active = backend()
    active.set_schema(parse_zed(PARENT_CREATE_SCHEMA))
    yield active
    reset_backend()


def _owned_folder(active, actor: SubjectRef):
    from tests.testapp.models import Folder

    with sudo(reason="test.parent-create.fixture"):
        folder = Folder.objects.create(name="parent")
    active.write_relationships(
        [RelationshipTuple(ObjectRef("blog/folder", str(folder.pk)), "owner", actor)]
    )
    return folder


@pytest.mark.django_db
def test_parent_arrow_create_uses_the_proposed_forward_relation(parent_create_backend) -> None:
    from tests.testapp.models import Post

    actor = _user("allowed")
    folder = _owned_folder(parent_create_backend, actor)

    created = Post.objects.with_actor(actor).create(title="allowed", folder=folder)
    assert created.folder_id == folder.pk

    with pytest.raises(PermissionDenied):
        Post.objects.with_actor(_user("denied")).create(title="denied", folder=folder)


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["save", "create", "insert"])
@pytest.mark.parametrize("allowed", [False, True])
def test_direct_save_and_queryset_create_share_candidate_preflight(
    parent_create_backend, operation, allowed
) -> None:
    from rebac import preflight
    from tests.testapp.models import Post

    folder = _owned_folder(parent_create_backend, _user("allowed"))
    actor = _user("allowed" if allowed else "denied")
    candidate = Post(title="candidate", folder=folder)

    def persist():
        if operation == "save":
            candidate.with_actor(actor).save()
            return candidate
        queryset = Post.objects.with_actor(actor)
        if operation == "create":
            return queryset.create(title=candidate.title, folder=folder)
        return queryset.insert(candidate)

    with patch("rebac.preflight.check_new", wraps=preflight.check_new) as check:
        if allowed:
            result = persist()
            assert result.pk is not None
            assert result.actor() == actor
        else:
            with pytest.raises(PermissionDenied):
                persist()
            assert candidate.pk is None
        check.assert_called_once()
        assert check.call_args.kwargs["subject"] == actor
        assert check.call_args.kwargs["relationships"] == {
            "folder": (SubjectRef.of("blog/folder", str(folder.pk)),)
        }
    assert Post.objects.sudo(reason="test.verify").count() == int(allowed)


@pytest.mark.django_db
def test_python_fk_default_is_resolved_before_create_preflight(
    parent_create_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.testapp.models import Post

    actor = _user("allowed")
    folder = _owned_folder(parent_create_backend, actor)
    monkeypatch.setattr(Post._meta.get_field("folder"), "_get_default", lambda: folder.pk)

    created = Post.objects.with_actor(actor).create(title="default parent")
    assert created.folder_id == folder.pk


@pytest.mark.django_db
def test_bulk_create_preflights_every_parent_before_any_insert(parent_create_backend) -> None:
    from tests.testapp.models import Folder, Post

    actor = _user("allowed")
    allowed = _owned_folder(parent_create_backend, actor)
    with sudo(reason="test.parent-create.fixture"):
        denied = Folder.objects.create(name="other")

    with pytest.raises(PermissionDenied):
        Post.objects.with_actor(actor).bulk_create(
            [
                Post(title="allowed", folder=allowed),
                Post(title="denied", folder=denied),
            ]
        )
    assert Post.objects.sudo(reason="test.verify").count() == 0


@pytest.mark.django_db
def test_many_valued_candidate_relation_fails_before_write(parent_create_backend) -> None:
    from tests.testapp.models import Post

    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/folder {}
            definition blog/post {
                relation collection: blog/folder // rebac:field=collections
                permission create = collection
            }
            """
        )
    )
    with pytest.raises(ValueError, match="direct forward ForeignKey or OneToOneField"):
        Post.objects.with_actor(_user("allowed")).create(title="unsupported")
    assert Post.objects.sudo(reason="test.verify").count() == 0


@pytest.mark.django_db
def test_reverse_candidate_relation_fails_before_write(parent_create_backend) -> None:
    from tests.testapp.models import Folder

    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/post {}
            definition blog/folder {
                relation post: blog/post // rebac:field=posts
                permission create = post
            }
            """
        )
    )
    with pytest.raises(ValueError, match="direct forward ForeignKey or OneToOneField"):
        Folder.objects.with_actor(_user("allowed")).create(name="unsupported")
    assert Folder.objects.sudo(reason="test.verify").count() == 0


@pytest.mark.django_db
def test_database_default_candidate_relation_fails_before_write(parent_create_backend) -> None:
    from django.db.models import Value
    from django.db.models.expressions import DatabaseDefault

    from tests.testapp.models import Post

    candidate = Post(title="database default")
    candidate.folder_id = DatabaseDefault(Value(None))
    with pytest.raises(ValueError, match="expression-backed relationships"):
        Post.objects.with_actor(_user("allowed")).bulk_create([candidate])
    assert Post.objects.sudo(reason="test.verify").count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["bulk_create", "insert"])
def test_bulk_candidate_lookup_and_insert_share_write_router_alias(
    parent_create_backend, operation
) -> None:
    from tests.testapp.models import VirtualFolder, VirtualPost

    actor = _user("allowed")
    with sudo(reason="test.parent-create.fixture"):
        folder = VirtualFolder.objects.create(name="routed parent")
    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition test/virtualfolder {
                relation owner: auth/user
                permission write = owner
            }
            definition test/virtualpost {
                relation folder: test/virtualfolder // rebac:field=folder
                permission create = folder->write
            }
            """
        )
    )
    parent_create_backend.write_relationships(
        [
            RelationshipTuple(
                ObjectRef("test/virtualfolder", folder.virtual_id),
                "owner",
                actor,
            )
        ]
    )

    with override_settings(DATABASE_ROUTERS=[CreateWriteRouter()]):
        queryset = VirtualPost.objects.with_actor(actor)
        candidate = VirtualPost(title="routed", folder_id=folder.pk)
        if operation == "bulk_create":
            row = queryset.bulk_create([candidate])[0]
        else:
            row = queryset.insert(candidate)

    assert row._state.db == "default"
    assert VirtualPost.objects.sudo(reason="test.verify").filter(pk=row.pk).exists()


@pytest.mark.django_db
def test_adding_instance_with_existing_pk_cannot_update_after_create_gate(
    _global_backend,
) -> None:
    from django.db import IntegrityError, transaction

    from tests.testapp.models import Post

    with sudo(reason="test.fixture"):
        stored = Post.objects.create(title="stored")

    replacement = Post(pk=stored.pk, title="overwritten").with_actor(_user("alice"))
    with pytest.raises(IntegrityError), transaction.atomic():
        replacement.save()

    assert Post.objects.sudo(reason="test.verify").get(pk=stored.pk).title == "stored"


@pytest.mark.django_db
@pytest.mark.parametrize("save_kwargs", [{"force_update": True}, {"update_fields": ["title"]}])
def test_adding_instance_rejects_update_only_save_options(_global_backend, save_kwargs) -> None:
    from tests.testapp.models import Post

    candidate = Post(title="new").with_actor(_user("alice"))
    with pytest.raises(ValueError, match="must be inserted"):
        candidate.save(**save_kwargs)
    assert not Post.objects.sudo(reason="test.verify").exists()


@pytest.mark.django_db
def test_candidate_fk_identity_uses_native_target_field_normalization(
    parent_create_backend,
) -> None:
    from tests.testapp.models import Post

    actor = _user("allowed")
    folder = _owned_folder(parent_create_backend, actor)
    candidate = Post(title="normalized")
    candidate.folder_id = f"0{folder.pk}"

    candidate.with_actor(actor).save()

    assert Post.objects.sudo(reason="test.verify").get(pk=candidate.pk).folder_id == folder.pk


@pytest.mark.django_db
def test_non_direct_candidate_identity_uses_raw_fk_on_write_alias_despite_stale_cache(
    parent_create_backend,
) -> None:
    from tests.testapp.models import VirtualFolder, VirtualPost

    actor = _user("allowed")
    with sudo(reason="test.fixture"):
        stale = VirtualFolder.objects.create(name="stale")
        intended = VirtualFolder.objects.create(name="intended")
    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition test/virtualfolder {
                relation owner: auth/user
                permission write = owner
            }
            definition test/virtualpost {
                relation folder: test/virtualfolder // rebac:field=folder
                permission create = folder->write
            }
            """
        )
    )
    parent_create_backend.write_relationships(
        [RelationshipTuple(ObjectRef("test/virtualfolder", intended.virtual_id), "owner", actor)]
    )
    candidate = VirtualPost(title="raw fk wins", folder=stale)
    candidate.__dict__["folder_id"] = intended.pk

    with override_settings(DATABASE_ROUTERS=[CreateWriteRouter()]):
        candidate.with_actor(actor).save()

    assert candidate.folder_id == intended.pk


@pytest.mark.django_db
def test_bulk_create_rejects_base_proxy_model_mismatches_before_preflight(
    _global_backend,
) -> None:
    from tests.testapp.models import Post, VirtualPost

    with sudo(reason="test.exact-model"):
        with pytest.raises(TypeError, match="exact Post instances"):
            Post.objects.bulk_create([VirtualPost(title="proxy")])
        with pytest.raises(TypeError, match="exact VirtualPost instances"):
            VirtualPost.objects.bulk_create([Post(title="base")])

    assert not Post.objects.sudo(reason="test.verify").exists()


@pytest.mark.django_db
def test_insert_only_save_preserves_native_multi_table_parent_attachment(_global_backend) -> None:
    from tests.testapp.models import NativeParentLinkedChild, NativeParentLinkedResource

    owner = _django_user("owner")
    with sudo(reason="test.multitable"):
        parent = NativeParentLinkedResource.objects.create(name="existing parent")
        child = NativeParentLinkedChild(
            nativeparentlinkedresource_ptr=parent,
            name=parent.name,
            owner=owner,
        )
        child.save()

    assert child.pk == parent.pk
    assert NativeParentLinkedChild.objects.sudo(reason="test.verify").filter(pk=parent.pk).exists()


@pytest.mark.django_db
def test_actor_child_create_cannot_update_an_existing_multi_table_parent(
    parent_create_backend,
) -> None:
    from django.db import IntegrityError, transaction

    from tests.testapp.models import NativeParentLinkedChild, NativeParentLinkedResource

    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition test/nativeparentlinkedresource {
                permission write = anonymous
            }
            definition test/nativeparentlinkedchild {
                permission create = authenticated
            }
            """
        )
    )
    owner = _django_user("mti-owner")
    with sudo(reason="test.fixture"):
        parent = NativeParentLinkedResource.objects.create(name="protected parent")
    child = NativeParentLinkedChild(
        nativeparentlinkedresource_ptr=parent,
        name="overwritten",
        owner=owner,
    ).with_actor(_user("child-creator"))

    with pytest.raises(IntegrityError), transaction.atomic():
        child.save()

    stored = NativeParentLinkedResource.objects.sudo(reason="test.verify").get(pk=parent.pk)
    assert stored.name == "protected parent"
