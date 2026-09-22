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
    NoActorResolvedError,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SchemaError,
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


@pytest.fixture
def non_resource_models(transactional_db):
    from django.db import connection, models
    from django.test.utils import isolate_apps

    from rebac import RebacMixin
    from rebac.resources import model_resource_type

    with isolate_apps("tests.testapp"):

        class HelperRecord(RebacMixin, models.Model):
            name = models.CharField(max_length=100)
            # Helper tables use Django's ordinary manager, without REBAC scope.
            objects = models.Manager()

            class Meta:
                app_label = "testapp"

        class HelperChild(HelperRecord):
            detail = models.CharField(max_length=100)

            class Meta:
                app_label = "testapp"

        assert model_resource_type(HelperRecord) is None
        assert model_resource_type(HelperChild) is None
        with connection.schema_editor() as editor:
            editor.create_model(HelperRecord)
            editor.create_model(HelperChild)
        try:
            yield HelperRecord, HelperChild
        finally:
            with connection.schema_editor() as editor:
                editor.delete_model(HelperChild)
                editor.delete_model(HelperRecord)


@pytest.mark.parametrize("manager_name", ["objects", "_base_manager"])
@pytest.mark.parametrize("child", [False, True], ids=["record", "mti-child"])
def test_non_resource_create_needs_no_actor(non_resource_models, manager_name, child) -> None:
    model = non_resource_models[int(child)]

    row = getattr(model, manager_name).create(name="helper")

    assert row.pk is not None
    assert model._base_manager.get(pk=row.pk).name == "helper"


@pytest.mark.parametrize("save_kwargs", [{}, {"force_update": True}, {"update_fields": ["name"]}])
def test_non_resource_adding_instance_keeps_django_update_semantics(
    non_resource_models, save_kwargs
) -> None:
    model, _child_model = non_resource_models
    stored = model.objects.create(name="original")
    replacement = model(pk=stored.pk, name="updated")

    replacement.save(**save_kwargs)

    assert model.objects.count() == 1
    assert model.objects.get(pk=stored.pk).name == "updated"


def test_non_resource_child_can_attach_to_existing_parent(non_resource_models) -> None:
    parent_model, child_model = non_resource_models
    parent = parent_model.objects.create(name="parent")

    child = child_model.objects.create(helperrecord_ptr=parent, name="updated", detail="child")

    assert child.pk == parent.pk
    assert parent_model.objects.count() == 1
    assert parent_model.objects.get(pk=parent.pk).name == "updated"
    assert child_model.objects.get(pk=parent.pk).detail == "child"


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["save", "create", "base_create"])
def test_resource_create_still_requires_actor(operation) -> None:
    from tests.testapp.models import Post

    with pytest.raises(MissingActorError):
        if operation == "save":
            Post(title="forbidden").save()
        else:
            manager = Post.objects if operation == "create" else Post._base_manager
            manager.create(title="forbidden")
    assert not Post._base_manager.exists()


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


PROPOSED_RELATIONSHIPS_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    permission write = owner
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation contributor: auth/user
    permission contributor_create = contributor
    permission create = (folder->write & contributor_create)
}
"""


def test_model_proposed_relationships_default_is_empty() -> None:
    from tests.testapp.models import Post

    candidate = Post(title="no tuple contributions")
    assert candidate.proposed_relationships() == {}
    assert candidate.proposed_relationships(using="write-target") == {}


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["save", "create", "insert"])
@pytest.mark.parametrize("contributes", [False, True])
def test_model_proposed_contributor_merges_with_fields_in_each_create_path(
    parent_create_backend, monkeypatch: pytest.MonkeyPatch, operation, contributes
) -> None:
    from django.db import transaction

    from rebac import preflight
    from tests.testapp.models import Post

    actor = _user("allowed")
    folder = _owned_folder(parent_create_backend, actor)
    parent_create_backend.set_schema(parse_zed(PROPOSED_RELATIONSHIPS_SCHEMA))
    consulted = []

    def proposed_relationships(self, *, using: str | None = None):
        assert self._state.adding and self.pk is None
        assert using == "default"
        consulted.append(self)
        return {"contributor": iter([actor])} if self.body == "contributor" else {}

    original_save = Post.save

    def save(self, *args, **kwargs):
        with transaction.atomic(using="default"):
            original_save(self, *args, **kwargs)
            if self.body == "contributor":
                parent_create_backend.write_relationships(
                    [RelationshipTuple(ObjectRef("blog/post", str(self.pk)), "contributor", actor)]
                )

    monkeypatch.setattr(Post, "proposed_relationships", proposed_relationships)
    monkeypatch.setattr(Post, "save", save)
    candidate = Post(
        title="proposed tuple", folder=folder, body="contributor" if contributes else ""
    )

    def persist():
        if operation == "save":
            candidate.with_actor(actor).save()
            return candidate
        queryset = Post.objects.with_actor(actor)
        if operation == "create":
            return queryset.create(title=candidate.title, folder=folder, body=candidate.body)
        return queryset.insert(candidate)

    with (
        override_settings(DATABASE_ROUTERS=[CreateWriteRouter()]),
        patch("rebac.preflight.check_new", wraps=preflight.check_new) as check,
    ):
        if contributes:
            result = persist()
            assert result.pk is not None
            assert result.actor() == actor
            assert consulted == [result]
            assert parent_create_backend.has_access(
                subject=actor, action="contributor", resource=ObjectRef("blog/post", str(result.pk))
            )
        else:
            with pytest.raises(PermissionDenied):
                persist()
            assert consulted[0].pk is None
        assert len(consulted) == 1
        check.assert_called_once()
        expected = {"folder": (SubjectRef.of("blog/folder", str(folder.pk)),)}
        if contributes:
            expected["contributor"] = (actor,)
        assert check.call_args.kwargs["relationships"] == expected
    assert Post.objects.sudo(reason="test.verify").count() == int(contributes)


@pytest.mark.django_db
def test_check_new_wildcard_subject_round_trips_for_read(parent_create_backend) -> None:
    from rebac import check_new

    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/post {
                relation shared: auth/user:*
                permission read = shared
            }
            """
        )
    )
    wildcard = SubjectRef.of("auth/user", "*")
    assert SubjectRef.parse(str(wildcard)) == wildcard
    relationships = {"shared": [wildcard]}
    result = check_new(
        subject=_user("reader"),
        action="read",
        resource_type="blog/post",
        relationships=relationships,
        backend=parent_create_backend,
    )
    assert result.allowed
    assert relationships == {"shared": [wildcard]}


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["save", "create", "insert", "bulk_create"])
@pytest.mark.parametrize("referenced", [False, True])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("backing", ["field", "const"])
def test_model_proposed_library_owned_relation_raises_before_write(
    parent_create_backend, monkeypatch: pytest.MonkeyPatch, operation, referenced, empty, backing
) -> None:
    from tests.testapp.models import Post

    parent_create_backend.set_schema(
        parse_zed(
            PROPOSED_RELATIONSHIPS_SCHEMA.replace(
                "(folder->write & contributor_create)",
                "folder->write" if referenced else "authenticated",
            ).replace("rebac:field=folder", f"rebac:{backing}=folder")
        )
    )

    def proposed_relationships(self, *, using: str | None = None):
        assert using == "default"
        return {"folder": [] if empty else [SubjectRef.of("blog/folder", "forged")]}

    monkeypatch.setattr(Post, "proposed_relationships", proposed_relationships)
    candidate = Post(title="cannot replace field projection")
    queryset = Post.objects.with_actor(_user("allowed"))
    with pytest.raises(
        SchemaError, match=r"proposed_relationships\(\) must not supply library-owned relations"
    ):
        if operation == "save":
            candidate.with_actor(_user("allowed")).save()
        elif operation == "create":
            queryset.create(title=candidate.title)
        elif operation == "insert":
            queryset.insert(candidate)
        else:
            queryset.bulk_create([candidate])
    assert candidate.pk is None
    assert Post.objects.sudo(reason="test.verify").count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("relation", ["missing", "create"])
@pytest.mark.parametrize("empty", [False, True])
def test_model_proposed_unknown_relation_raises_before_write(
    parent_create_backend, monkeypatch: pytest.MonkeyPatch, relation, empty
) -> None:
    from tests.testapp.models import Post

    parent_create_backend.set_schema(parse_zed(INTEGRATION_SCHEMA))

    def proposed_relationships(self, *, using: str | None = None):
        assert using == "default"
        return {relation: [] if empty else [_user("allowed")]}

    monkeypatch.setattr(Post, "proposed_relationships", proposed_relationships)
    with pytest.raises(SchemaError, match=relation):
        Post.objects.with_actor(_user("allowed")).create(title="invalid relation")
    assert Post.objects.sudo(reason="test.verify").count() == 0


@pytest.mark.django_db
def test_model_proposed_unreferenced_relation_does_not_evaluate_queryset(
    parent_create_backend, monkeypatch: pytest.MonkeyPatch, django_assert_num_queries
) -> None:
    from django.contrib.auth import get_user_model

    from rebac import preflight
    from tests.testapp.models import Post

    parent_create_backend.set_schema(
        parse_zed(
            PROPOSED_RELATIONSHIPS_SCHEMA.replace(
                "(folder->write & contributor_create)", "authenticated"
            )
        )
    )
    unreferenced_subjects = get_user_model().objects.all()

    def proposed_relationships(self, *, using: str | None = None):
        assert using == "write-target"
        return {"contributor": unreferenced_subjects}

    monkeypatch.setattr(Post, "proposed_relationships", proposed_relationships)
    with (
        django_assert_num_queries(0),
        patch("rebac.preflight.check_new", wraps=preflight.check_new) as check,
    ):
        result = preflight._check_new_model(
            Post(title="unused tuples"), subject=_user("allowed"), using="write-target"
        )
    assert result.allowed
    assert unreferenced_subjects._result_cache is None
    check.assert_called_once()
    assert check.call_args.kwargs["relationships"] == {}


@pytest.mark.django_db
@pytest.mark.parametrize("all_allowed", [False, True])
def test_bulk_create_checks_each_model_proposed_relationship_before_any_insert(
    parent_create_backend, monkeypatch: pytest.MonkeyPatch, all_allowed
) -> None:
    from tests.testapp.models import Post

    actor = _user("allowed")
    folder = _owned_folder(parent_create_backend, actor)
    parent_create_backend.set_schema(parse_zed(PROPOSED_RELATIONSHIPS_SCHEMA))
    consulted = []

    def proposed_relationships(self, *, using: str | None = None):
        assert using == "default"
        consulted.append(self)
        assert self.pk is None
        assert Post.objects.sudo(reason="test.before-insert").count() == 0
        return {"contributor": [actor]} if self.body == "contributor" else {}

    monkeypatch.setattr(Post, "proposed_relationships", proposed_relationships)
    candidates = [
        Post(title="first", body="contributor", folder=folder),
        Post(title="second", body="contributor" if all_allowed else "", folder=folder),
    ]
    queryset = Post.objects.with_actor(actor)
    if all_allowed:
        rows = queryset.bulk_create(candidates)
        assert rows == candidates
        assert all(row.pk is not None and row.actor() == actor for row in rows)
    else:
        with pytest.raises(PermissionDenied):
            queryset.bulk_create(candidates)
        assert all(candidate.pk is None for candidate in candidates)
    assert consulted == candidates
    assert Post.objects.sudo(reason="test.verify").count() == (2 if all_allowed else 0)


@pytest.mark.django_db
@pytest.mark.parametrize("subject_kind", ["user", "subject-set"])
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_model_proposed_instances_use_canonical_subject_resolution(
    parent_create_backend, monkeypatch: pytest.MonkeyPatch, subject_kind, settings, storage
) -> None:
    from rebac import preflight
    from tests.testapp.models import Post, SubjectContainer

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/subjectcontainer { relation member: auth/user }
            definition blog/post {
                relation owner: auth/user | blog/subjectcontainer#member
                permission create = owner
            }
            """
        )
    )
    user = _django_user("proposed-owner")
    actor = _user(str(user.pk))
    subject = user
    expected = actor
    if subject_kind == "subject-set":
        subject = SubjectContainer.objects.sudo(reason="test.fixture").create(
            slug="members", title="subject set"
        )
        expected = SubjectRef.of("blog/subjectcontainer", "members", "member")
        parent_create_backend.write_relationships(
            [RelationshipTuple(expected.object, "member", actor)]
        )

    def proposed_relationships(self, *, using: str | None = None):
        assert using == "default"
        return {"owner": [subject]}

    monkeypatch.setattr(Post, "proposed_relationships", proposed_relationships)
    with patch("rebac.preflight.check_new", wraps=preflight.check_new) as check:
        created = Post.objects.with_actor(actor).create(title="model-owned subjects")
    assert created.pk is not None
    check.assert_called_once()
    assert check.call_args.kwargs["relationships"] == {"owner": (expected,)}


@pytest.mark.django_db
@pytest.mark.parametrize("subject_kind", ["unsaved-user", "unsaved-resource", "unresolvable"])
def test_model_proposed_invalid_subject_raises_before_write(
    parent_create_backend, monkeypatch: pytest.MonkeyPatch, subject_kind
) -> None:
    from django.contrib.auth import get_user_model
    from django.contrib.contenttypes.models import ContentType

    from tests.testapp.models import Post

    parent_create_backend.set_schema(
        parse_zed(INTEGRATION_SCHEMA.replace("authenticated", "owner"))
    )
    if subject_kind == "unsaved-user":
        subject = get_user_model()(username="unsaved")
    elif subject_kind == "unsaved-resource":
        subject = Post(title="unsaved subject")
    else:
        subject = ContentType.objects.get_for_model(Post)

    def proposed_relationships(self, *, using: str | None = None):
        assert using == "default"
        return {"owner": [subject]}

    monkeypatch.setattr(Post, "proposed_relationships", proposed_relationships)
    with pytest.raises(NoActorResolvedError):
        Post.objects.with_actor(_user("allowed")).create(title="invalid contributed subject")
    assert Post.objects.sudo(reason="test.verify").count() == 0


@pytest.mark.django_db
def test_parent_arrow_create_uses_the_proposed_forward_relation(parent_create_backend) -> None:
    from tests.testapp.models import Post

    actor = _user("allowed")
    folder = _owned_folder(parent_create_backend, actor)

    created = Post.objects.with_actor(actor).create(title="allowed", folder=folder)
    assert created.folder_id == folder.pk

    with pytest.raises(PermissionDenied):
        Post.objects.with_actor(_user("denied")).create(title="denied", folder=folder)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "operation, count", [("create", 1), ("bulk_create", 1), ("bulk_create", 4)]
)
def test_unreferenced_dangling_relations_create_without_projection_queries(
    parent_create_backend, operation, count
) -> None:
    from django.db import connection, models
    from django.test.utils import CaptureQueriesContext, isolate_apps

    from rebac import RebacMixin, field_backing, preflight
    from rebac.resources import model_for_resource_type
    from tests.testapp.models import Folder

    with isolate_apps("tests.testapp"):

        class UnreferencedCreateCandidate(RebacMixin, models.Model):
            # An unconstrained test-only field permits a genuinely dangling FK
            # on every database without changing the production model schema.
            folder = models.ForeignKey(Folder, on_delete=models.DO_NOTHING, db_constraint=False)

            class Meta:
                app_label = "testapp"
                rebac_resource_type = "test/unreferencedcreatecandidate"

        parent_create_backend.set_schema(
            parse_zed(
                """
                definition auth/user {}
                definition test/virtualfolder { permission read = authenticated }
                definition test/unreferencedcreatecandidate {
                    relation folder: test/virtualfolder // rebac:field=folder
                    relation filtered: test/virtualfolder // rebac:field={"path":"folder","filters":{"folder__is_active":true}}
                    relation ancestor: test/virtualfolder // rebac:field=folder__parent
                    permission read = (folder->read + filtered->read) + ancestor->read
                    permission create = authenticated
                }
                """
            )
        )

        def resolve_model(resource_type):
            if resource_type == "test/unreferencedcreatecandidate":
                return UnreferencedCreateCandidate
            return model_for_resource_type(resource_type)

        project = field_backing._proposed_forward_relationships

        def project_without_queries(*args, **kwargs):
            with CaptureQueriesContext(connection) as queries:
                relationships = project(*args, **kwargs)
            assert len(queries) == 0
            assert relationships == {}
            return relationships

        with connection.schema_editor() as editor:
            editor.create_model(UnreferencedCreateCandidate)
        try:
            with (
                patch("rebac.field_backing.model_for_resource_type", side_effect=resolve_model),
                patch(
                    "rebac.field_backing._proposed_forward_relationships",
                    side_effect=project_without_queries,
                ) as projection,
                patch("rebac.preflight.check_new", wraps=preflight.check_new) as check,
                CaptureQueriesContext(connection) as queries,
            ):
                queryset = UnreferencedCreateCandidate.objects.with_actor(_user("allowed"))
                if operation == "create":
                    rows = [queryset.create(folder_id=999_999)]
                else:
                    rows = queryset.bulk_create(
                        [UnreferencedCreateCandidate(folder_id=999_999) for _ in range(count)]
                    )
            assert projection.call_count == check.call_count == count
            assert all(call.kwargs["relationships"] == {} for call in check.call_args_list)
            assert not any(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
            assert len(rows) == count
            assert UnreferencedCreateCandidate._base_manager.count() == count
        finally:
            with connection.schema_editor() as editor:
                editor.delete_model(UnreferencedCreateCandidate)


@pytest.mark.django_db
def test_direct_identity_candidate_projection_issues_no_queries(
    parent_create_backend, django_assert_num_queries
) -> None:
    from rebac import preflight
    from tests.testapp.models import AuthoredPost

    author = _django_user("direct-identity-author")
    actor = _user(str(author.pk))
    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/authoredpost {
                relation author: auth/user // rebac:field=author
                permission create = author
            }
            """
        )
    )
    candidate = AuthoredPost(title="direct identity", author_id=f"0{author.pk}")
    with (
        patch("rebac.preflight.check_new", wraps=preflight.check_new) as check,
        django_assert_num_queries(0),
    ):
        result = preflight._check_new_model(candidate, subject=actor, using="default")
    assert result.allowed
    check.assert_called_once()
    assert check.call_args.kwargs["relationships"] == {"author": (actor,)}


@pytest.mark.django_db
@pytest.mark.parametrize("allowed", [False, True])
def test_candidate_projection_includes_transitive_permission_arrow_dependencies(
    parent_create_backend, allowed
) -> None:
    from tests.testapp.models import Post

    folder = _owned_folder(parent_create_backend, _user("allowed"))
    parent_create_backend.set_schema(
        parse_zed(
            PARENT_CREATE_SCHEMA.replace(
                "permission create = folder->write",
                """
                permission from_folder = folder->write
                permission can_create = from_folder
                permission create = can_create
                """,
            )
        )
    )
    _assert_candidate_create(
        Post,
        {"folder": (SubjectRef.of("blog/folder", str(folder.pk)),)},
        allowed,
        actor=_user("allowed" if allowed else "denied"),
        title="transitive create permission",
        folder=folder,
    )


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


def _assert_create_with_empty_relation(
    model, relation, requires_relation, *, bulk=False, **fields
) -> None:
    from rebac import preflight

    queryset = model.objects.with_actor(_user("allowed"))

    def persist():
        if bulk:
            return queryset.bulk_create([model(**fields)])[0]
        return queryset.create(**fields)

    with patch("rebac.preflight.check_new", wraps=preflight.check_new) as check:
        if requires_relation:
            with pytest.raises(PermissionDenied):
                persist()
        else:
            created = persist()
            assert created.pk is not None
        check.assert_called_once()
        assert check.call_args.kwargs["relationships"] == (
            {relation: ()} if requires_relation else {}
        )

    assert model.objects.sudo(reason="test.verify").count() == int(not requires_relation)


@pytest.mark.django_db
@pytest.mark.parametrize("requires_relation", [False, True], ids=["unused", "required"])
def test_many_valued_candidate_relation_is_empty_at_create_time(
    parent_create_backend, requires_relation
) -> None:
    from tests.testapp.models import Post

    create_permission = "collection->write" if requires_relation else "authenticated"
    parent_create_backend.set_schema(
        parse_zed(
            f"""
            definition auth/user {{}}
            definition blog/folder {{ permission write = authenticated }}
            definition blog/post {{
                relation collection: blog/folder // rebac:field=collections
                permission read = collection->write
                permission create = {create_permission}
            }}
            """
        )
    )
    _assert_create_with_empty_relation(Post, "collection", requires_relation, title="many-valued")


@pytest.mark.django_db
@pytest.mark.parametrize("requires_relation", [False, True], ids=["unused", "required"])
def test_reverse_candidate_relation_is_empty_at_create_time(
    parent_create_backend, requires_relation
) -> None:
    from tests.testapp.models import Folder

    create_permission = "post->write" if requires_relation else "authenticated"
    parent_create_backend.set_schema(
        parse_zed(
            f"""
            definition auth/user {{}}
            definition blog/post {{ permission write = authenticated }}
            definition blog/folder {{
                relation post: blog/post // rebac:field=posts
                permission read = post->write
                permission create = {create_permission}
            }}
            """
        )
    )
    _assert_create_with_empty_relation(Folder, "post", requires_relation, name="reverse")


@pytest.mark.django_db
@pytest.mark.parametrize("requires_relation", [False, True], ids=["unused", "required"])
def test_reverse_multihop_candidate_relation_is_empty_at_create_time(
    parent_create_backend, requires_relation
) -> None:
    from tests.testapp.models import Folder

    create_permission = "ancestor->write" if requires_relation else "authenticated"
    parent_create_backend.set_schema(
        parse_zed(
            f"""
            definition auth/user {{}}
            definition blog/folder {{
                relation ancestor: blog/folder // rebac:field=posts__folder
                permission write = authenticated
                permission create = {create_permission}
            }}
            """
        )
    )
    _assert_create_with_empty_relation(
        Folder, "ancestor", requires_relation, name="reverse first hop"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("requires_relation", [False, True], ids=["unused", "required"])
def test_reverse_one_to_one_candidate_relation_is_empty_at_create_time(
    parent_create_backend, requires_relation
) -> None:
    from tests.testapp.models import NativeParentLinkedResource

    create_permission = "child->write" if requires_relation else "authenticated"
    parent_create_backend.set_schema(
        parse_zed(
            f"""
            definition auth/user {{}}
            definition test/nativeparentlinkedchild {{ permission write = authenticated }}
            definition test/nativeparentlinkedresource {{
                relation child: test/nativeparentlinkedchild // rebac:field=nativeparentlinkedchild
                permission create = {create_permission}
            }}
            """
        )
    )
    _assert_create_with_empty_relation(
        NativeParentLinkedResource, "child", requires_relation, name="reverse one-to-one"
    )


def _assert_candidate_create(
    model, relationships, allowed, *, actor=None, bulk=False, **fields
) -> None:
    from rebac import preflight

    queryset = model.objects.with_actor(actor or _user("allowed"))

    def persist():
        if bulk:
            return queryset.bulk_create([model(**fields)])[0]
        return queryset.create(**fields)

    with patch("rebac.preflight.check_new", wraps=preflight.check_new) as check:
        if allowed:
            assert persist().pk is not None
        else:
            with pytest.raises(PermissionDenied):
                persist()
        check.assert_called_once()
        assert check.call_args.kwargs["relationships"] == relationships

    assert model.objects.using("default").sudo(reason="test.verify").count() == int(allowed)


@pytest.mark.django_db
@pytest.mark.parametrize("filter_matches", [False, True], ids=["filtered-out", "included"])
@pytest.mark.parametrize(
    "create_permission",
    [
        "authenticated",
        "folder->write",
        "(authenticated - folder->write)",
        "(authenticated & folder->write)",
    ],
    ids=["unused", "positive", "exclusion", "intersection"],
)
def test_filtered_candidate_relation_projects_the_write_alias_target(
    parent_create_backend, create_permission, filter_matches
) -> None:
    from tests.testapp.models import Folder, Post

    folder = _owned_folder(parent_create_backend, _user("allowed"))
    # Leave the cached Python target stale: predicates must read the write alias.
    Folder._base_manager.filter(pk=folder.pk).update(is_active=filter_matches)
    parent_create_backend.set_schema(
        parse_zed(
            f"""
            definition auth/user {{}}
            definition blog/folder {{
                relation owner: auth/user
                permission write = owner
            }}
            definition blog/post {{
                relation folder: blog/folder // rebac:field={{"path":"folder","filters":{{"folder__is_active":true}}}}
                permission read = folder->write
                permission create = {create_permission}
            }}
            """
        )
    )
    projected = (SubjectRef.of("blog/folder", str(folder.pk)),) if filter_matches else ()
    allowed = (
        True
        if create_permission == "authenticated"
        else not filter_matches
        if " - " in create_permission
        else filter_matches
    )
    with override_settings(DATABASE_ROUTERS=[CreateWriteRouter()]):
        _assert_candidate_create(
            Post,
            {} if create_permission == "authenticated" else {"folder": projected},
            allowed,
            title="filtered",
            folder=folder,
        )


@pytest.mark.django_db
@pytest.mark.parametrize("title", ["blocked", "allowed"])
def test_filtered_candidate_relation_uses_candidate_scalar_fields(
    parent_create_backend, title
) -> None:
    from tests.testapp.models import Post

    folder = _owned_folder(parent_create_backend, _user("allowed"))
    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/folder {
                relation owner: auth/user
                permission write = owner
            }
            definition blog/post {
                relation folder: blog/folder // rebac:field={"path":"folder","filters":{"title":"blocked"}}
                permission create = (authenticated - folder->write)
            }
            """
        )
    )
    projected = (SubjectRef.of("blog/folder", str(folder.pk)),) if title == "blocked" else ()
    _assert_candidate_create(
        Post, {"folder": projected}, title == "allowed", title=title, folder=folder
    )


@pytest.mark.django_db
@pytest.mark.parametrize("author_is_active", [False, True], ids=["blocked-author", "active-author"])
def test_filtered_author_exclusion_denies_the_blocked_actor(
    parent_create_backend, author_is_active
) -> None:
    from tests.testapp.models import AuthoredPost

    author = _django_user("author")
    type(author)._base_manager.filter(pk=author.pk).update(is_active=author_is_active)
    actor = _user(str(author.pk))
    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/authoredpost {
                relation blocked: auth/user // rebac:field={"path":"author","filters":{"author__is_active":false}}
                permission create = (authenticated - blocked)
            }
            """
        )
    )
    with override_settings(DATABASE_ROUTERS=[CreateWriteRouter()]):
        _assert_candidate_create(
            AuthoredPost,
            {"blocked": () if author_is_active else (actor,)},
            author_is_active,
            actor=actor,
            title="filtered author exclusion",
            author=author,
        )


@pytest.mark.django_db
@pytest.mark.parametrize("actor_is_owner", [False, True], ids=["other-owner", "actor-owner"])
@pytest.mark.parametrize(
    "create_permission",
    [
        "authenticated",
        "ancestor->write",
        "(authenticated - ancestor->write)",
        "(authenticated & ancestor->write)",
    ],
    ids=["unused", "positive", "exclusion", "intersection"],
)
def test_multihop_candidate_relation_projects_the_write_alias_target(
    parent_create_backend, create_permission, actor_is_owner
) -> None:
    from tests.testapp.models import Folder, Post

    owner = _user("allowed") if actor_is_owner else _user("other")
    parent = _owned_folder(parent_create_backend, owner)
    folder = Folder.objects.sudo(reason="test.fixture").create(name="child", parent=parent)
    parent_create_backend.set_schema(
        parse_zed(
            f"""
            definition auth/user {{}}
            definition blog/folder {{
                relation owner: auth/user
                permission write = owner
            }}
            definition blog/post {{
                relation ancestor: blog/folder // rebac:field=folder__parent
                permission read = ancestor->write
                permission create = {create_permission}
            }}
            """
        )
    )
    allowed = (
        True
        if create_permission == "authenticated"
        else not actor_is_owner
        if " - " in create_permission
        else actor_is_owner
    )
    with override_settings(DATABASE_ROUTERS=[CreateWriteRouter()]):
        _assert_candidate_create(
            Post,
            {}
            if create_permission == "authenticated"
            else {"ancestor": (SubjectRef.of("blog/folder", str(parent.pk)),)},
            allowed,
            title="multi-hop",
            folder_id=folder.pk,
        )


@pytest.mark.django_db
@pytest.mark.parametrize("filter_matches", [False, True], ids=["filtered-out", "included"])
def test_multihop_candidate_relation_filters_the_first_hop_target(
    parent_create_backend, filter_matches
) -> None:
    from tests.testapp.models import Folder, Post

    parent = _owned_folder(parent_create_backend, _user("allowed"))
    folder = Folder.objects.sudo(reason="test.fixture").create(name="first hop", parent=parent)
    Folder._base_manager.filter(pk=folder.pk).update(is_active=filter_matches)
    # Opposite values ensure evaluating the filter on the final target changes
    # the decision. Cached Python values must not override the write alias.
    Folder._base_manager.filter(pk=parent.pk).update(is_active=not filter_matches)
    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/folder {
                relation owner: auth/user
                permission write = owner
            }
            definition blog/post {
                relation ancestor: blog/folder // rebac:field={"path":"folder__parent","filters":{"folder__is_active":true}}
                permission create = (authenticated - ancestor->write)
            }
            """
        )
    )
    projected = (SubjectRef.of("blog/folder", str(parent.pk)),) if filter_matches else ()
    with override_settings(DATABASE_ROUTERS=[CreateWriteRouter()]):
        _assert_candidate_create(
            Post,
            {"ancestor": projected},
            not filter_matches,
            title="filtered multi-hop exclusion",
            folder=folder,
        )


@pytest.mark.django_db
@pytest.mark.parametrize("requires_relation", [False, True], ids=["unused", "exclusion"])
def test_forward_then_reverse_candidate_relation_is_unknown_at_create_time(
    parent_create_backend, requires_relation
) -> None:
    from tests.testapp.models import Post

    folder = _owned_folder(parent_create_backend, _user("allowed"))
    create_permission = "(authenticated - sibling->write)" if requires_relation else "authenticated"
    parent_create_backend.set_schema(
        parse_zed(
            f"""
            definition auth/user {{}}
            definition blog/post {{
                relation sibling: blog/post // rebac:field=folder__posts
                permission write = authenticated
                permission create = {create_permission}
            }}
            """
        )
    )
    _assert_candidate_create(
        Post,
        {"sibling": None} if requires_relation else {},
        not requires_relation,
        title="persisted target reverse path",
        folder=folder,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "create_permission",
    [
        "authenticated",
        "folder->write",
        "(authenticated - folder->write)",
        "(authenticated & folder->write)",
    ],
    ids=["unused", "positive", "exclusion", "intersection"],
)
@pytest.mark.parametrize("database_default", [False, True], ids=["expression", "database-default"])
@pytest.mark.parametrize("bulk", [False, True], ids=["create", "bulk-create"])
def test_database_default_candidate_relation_is_unknown_at_create_time(
    parent_create_backend, create_permission, database_default, bulk
) -> None:
    from django.db.models import Value
    from django.db.models.expressions import DatabaseDefault

    from tests.testapp.models import Post

    folder = _owned_folder(parent_create_backend, _user("allowed"))
    parent_create_backend.set_schema(
        parse_zed(
            f"""
            definition auth/user {{}}
            definition blog/folder {{ permission write = authenticated }}
            definition blog/post {{
                relation folder: blog/folder // rebac:field=folder
                permission read = folder->write
                permission create = {create_permission}
            }}
            """
        )
    )
    value = DatabaseDefault(Value(folder.pk)) if database_default else Value(folder.pk)
    _assert_candidate_create(
        Post,
        {} if create_permission == "authenticated" else {"folder": None},
        create_permission == "authenticated",
        bulk=bulk,
        title="database expression",
        folder_id=value,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "create_permission",
    [
        "authenticated",
        "folder->write",
        "(authenticated - folder->write)",
        "(authenticated & folder->write)",
    ],
    ids=["unused", "positive", "exclusion", "intersection"],
)
@pytest.mark.parametrize("expression_name", ["F", "OuterRef"])
def test_candidate_field_reference_expression_is_unknown_in_preflight(
    parent_create_backend, create_permission, expression_name
) -> None:
    from django.db import models

    from rebac import preflight
    from tests.testapp.models import Post

    parent_create_backend.set_schema(
        parse_zed(PARENT_CREATE_SCHEMA.replace("folder->write", create_permission))
    )
    candidate = Post(title="field reference", folder_id=getattr(models, expression_name)("folder"))
    # Column references are not valid INSERT values; exercise their authorization
    # projection before Django's independent SQL validation can reject them.
    with patch("rebac.preflight.check_new", wraps=preflight.check_new) as check:
        result = preflight._check_new_model(candidate, subject=_user("allowed"), using="default")
        assert result.allowed is (create_permission == "authenticated")
        check.assert_called_once()
        assert check.call_args.kwargs["relationships"] == (
            {} if create_permission == "authenticated" else {"folder": None}
        )
    assert candidate.pk is None
    assert Post.objects.sudo(reason="test.verify").count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("requires_relation", [False, True], ids=["unused", "exclusion"])
def test_insert_assigned_parent_link_is_unknown_at_create_time(
    parent_create_backend, requires_relation
) -> None:
    from tests.testapp.models import NativeParentLinkedChild, NativeParentLinkedResource

    owner = _django_user("parent-link-owner")
    create_permission = "(authenticated - parent->write)" if requires_relation else "authenticated"
    parent_create_backend.set_schema(
        parse_zed(
            f"""
            definition auth/user {{}}
            definition test/nativeparentlinkedresource {{ permission write = authenticated }}
            definition test/nativeparentlinkedchild {{
                relation parent: test/nativeparentlinkedresource // rebac:field=nativeparentlinkedresource_ptr
                permission create = {create_permission}
            }}
            """
        )
    )
    _assert_candidate_create(
        NativeParentLinkedChild,
        {"parent": None} if requires_relation else {},
        not requires_relation,
        name="insert-assigned parent",
        owner=owner,
    )
    assert NativeParentLinkedResource.objects.sudo(reason="test.verify").count() == int(
        not requires_relation
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("actor_is_owner", [False, True], ids=["other-owner", "actor-owner"])
@pytest.mark.parametrize(
    "create_permission",
    ["owner", "(authenticated - owner)", "(authenticated & owner)"],
    ids=["positive", "exclusion", "intersection"],
)
def test_multi_table_child_projects_parent_declared_forward_fk(
    parent_create_backend, create_permission, actor_is_owner
) -> None:
    from django.db import connection
    from django.test.utils import isolate_apps

    from rebac.resources import model_for_resource_type
    from tests.testapp.models import NativeParentLinkedChild, NativeParentLinkedResource

    owner = _django_user("parent-owner")
    actor = _user(str(owner.pk)) if actor_is_owner else _user("other")
    allowed = not actor_is_owner if " - " in create_permission else actor_is_owner

    with isolate_apps("tests.testapp"):

        class CreateCandidateChild(NativeParentLinkedChild):
            class Meta:
                app_label = "testapp"
                rebac_resource_type = "test/createcandidatechild"

        parent_create_backend.set_schema(
            parse_zed(
                f"""
                definition auth/user {{}}
                definition test/createcandidatechild {{
                    relation owner: auth/user // rebac:field=owner
                    permission create = {create_permission}
                }}
                """
            )
        )
        assert CreateCandidateChild._meta.get_field("owner").model is NativeParentLinkedChild

        def resolve_model(resource_type):
            if resource_type == "test/createcandidatechild":
                return CreateCandidateChild
            return model_for_resource_type(resource_type)

        with connection.schema_editor() as editor:
            editor.create_model(CreateCandidateChild)
        try:
            with patch("rebac.field_backing.model_for_resource_type", side_effect=resolve_model):
                _assert_candidate_create(
                    CreateCandidateChild,
                    {"owner": (_user(str(owner.pk)),)},
                    allowed,
                    actor=actor,
                    name="inherited foreign key",
                    owner=owner,
                )
            assert NativeParentLinkedChild.objects.sudo(reason="test.verify").count() == int(
                allowed
            )
            assert NativeParentLinkedResource.objects.sudo(reason="test.verify").count() == int(
                allowed
            )
        finally:
            with connection.schema_editor() as editor:
                editor.delete_model(CreateCandidateChild)


@pytest.mark.django_db
def test_candidate_missing_related_row_on_write_alias_still_raises(
    parent_create_backend,
) -> None:
    from tests.testapp.models import VirtualPost

    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition test/virtualfolder { permission write = authenticated }
            definition test/virtualpost {
                relation folder: test/virtualfolder // rebac:field=folder
                permission create = folder->write
            }
            """
        )
    )
    with override_settings(DATABASE_ROUTERS=[CreateWriteRouter()]):
        with pytest.raises(ValueError, match="proposed related object is unavailable"):
            VirtualPost.objects.with_actor(_user("allowed")).create(
                title="missing parent", folder_id=999_999
            )
    assert VirtualPost.objects.sudo(reason="test.verify").count() == 0


@pytest.mark.django_db
def test_candidate_unresolvable_backing_still_raises(parent_create_backend) -> None:
    from tests.testapp.models import Post

    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition blog/folder { permission write = authenticated }
            definition blog/post {
                relation folder: blog/folder // rebac:field=missing
                permission create = folder->write
            }
            """
        )
    )
    with pytest.raises(ValueError, match="field backing is not resolvable"):
        Post.objects.with_actor(_user("allowed")).create(title="invalid backing")
    assert Post.objects.sudo(reason="test.verify").count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("expression_name", ["Value", "F"])
def test_candidate_non_scalar_prepared_fk_identity_still_raises(
    parent_create_backend, monkeypatch: pytest.MonkeyPatch, expression_name
) -> None:
    from django.db import models

    from tests.testapp.models import Post

    folder = _owned_folder(parent_create_backend, _user("allowed"))
    monkeypatch.setattr(
        Post._meta.get_field("folder").target_field,
        "get_prep_value",
        lambda value: getattr(models, expression_name)(value),
    )
    with pytest.raises(ValueError, match="foreign-key identity did not resolve to a scalar"):
        Post.objects.with_actor(_user("allowed")).create(title="non-scalar parent", folder=folder)
    assert Post.objects.sudo(reason="test.verify").count() == 0


@pytest.mark.django_db
def test_candidate_related_target_without_rebac_identity_still_raises(
    parent_create_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.testapp.models import VirtualFolder, VirtualPost

    folder = VirtualFolder.objects.sudo(reason="test.fixture").create(name="missing identity")
    parent_create_backend.set_schema(
        parse_zed(
            """
            definition auth/user {}
            definition test/virtualfolder { permission write = authenticated }
            definition test/virtualpost {
                relation folder: test/virtualfolder // rebac:field=folder
                permission create = folder->write
            }
            """
        )
    )
    monkeypatch.setattr(VirtualFolder, "virtual_id", property(lambda self: None))
    with pytest.raises(ValueError, match="related object has no REBAC identity"):
        VirtualPost.objects.with_actor(_user("allowed")).create(
            title="unidentified parent", folder_id=folder.pk
        )
    assert VirtualPost.objects.sudo(reason="test.verify").count() == 0


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
