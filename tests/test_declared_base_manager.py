"""A model's declared base manager is kept, validated, and its writes are gated, and reads follow them."""

import pytest
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test.utils import isolate_apps

from rebac import (
    MissingActorError,
    PermissionDenied,
    RebacManager,
    RebacMixin,
    RebacTrackedMixin,
    TrackedManager,
    TrackedQuerySet,
    actor_context,
    sudo,
    to_object_ref,
)
from rebac.backends import backend
from rebac.backends.local import LocalBackend
from rebac.checks import check_declared_base_managers
from rebac.schema import parse_zed
from rebac.testing import install_schema
from rebac.types import RelationshipTuple, SubjectRef
from tests.testapp.models import DeclaredBasePost, Folder, OwnerQuerySet, Post

ALICE = SubjectRef.of("auth/user", "alice")

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}
definition test/declaredbasepost {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
"""

GATED_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}
definition test/declaredbasepost {
    relation folder: blog/folder // rebac:field=folder
    relation editor: auth/user
    permission read = folder->read
    permission write = editor
}
"""


class PolicyQuerySet(TrackedQuerySet):
    def update(self, **kwargs):
        raise TypeError("rows are immutable")


class PlainQuerySet(models.QuerySet):
    pass


class HidingManager(models.Manager.from_queryset(PolicyQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(hidden=False)


class UntrackedManager(models.Manager.from_queryset(PolicyQuerySet)):
    def get_queryset(self):
        return PlainQuerySet(self.model, using=self._db)


def _base(model):
    return model._meta.base_manager_name, model._base_manager._queryset_class


@isolate_apps("tests.testapp")
def test_declared_base_manager_is_kept():
    class Revision(RebacMixin, models.Model):
        system_objects = models.Manager.from_queryset(PolicyQuerySet)()

        class Meta:
            app_label = "testapp"
            rebac_resource_type = "test/revision"
            base_manager_name = "system_objects"

    assert _base(Revision) == ("system_objects", PolicyQuerySet)
    assert Revision._meta.base_manager is Revision._base_manager
    assert Revision._base_manager.name == "system_objects"
    assert Revision._meta.default_manager_name == "objects"
    assert isinstance(Revision._default_manager, RebacManager)


@isolate_apps("tests.testapp")
def test_base_manager_declared_on_a_plain_abstract_parent_is_kept():
    class AppendOnly(models.Model):
        _append_only_base = models.Manager.from_queryset(PolicyQuerySet)()

        class Meta:
            abstract = True
            base_manager_name = "_append_only_base"

    class MixinFirst(RebacMixin, AppendOnly):
        class Meta:
            app_label = "testapp"
            rebac_resource_type = "test/mixinfirst"

    class ParentFirst(AppendOnly, RebacMixin):
        class Meta:
            app_label = "testapp"
            rebac_resource_type = "test/parentfirst"

    class InheritedMeta(RebacMixin, AppendOnly):
        class Meta(AppendOnly.Meta):
            app_label = "testapp"
            rebac_resource_type = "test/inheritedmeta"

    for model in (MixinFirst, ParentFirst, InheritedMeta):
        assert _base(model) == ("_append_only_base", PolicyQuerySet)
        assert model._meta.base_manager is model._base_manager
        assert isinstance(model._default_manager, RebacManager)


@isolate_apps("tests.testapp")
def test_base_manager_declared_on_an_abstract_rebac_parent_is_kept():
    class Evidence(RebacTrackedMixin):
        system_objects = models.Manager.from_queryset(PolicyQuerySet)()

        class Meta:
            abstract = True
            base_manager_name = "system_objects"

    class Part(Evidence):
        class Meta:
            app_label = "testapp"

    class Page(Evidence):
        class Meta(Evidence.Meta):
            app_label = "testapp"

    class Chapter(Part):
        class Meta:
            app_label = "testapp"

    class Opted(Evidence):
        class Meta:
            app_label = "testapp"
            base_manager_name = "_rebac_base"

    for model in (Part, Page, Chapter):
        assert _base(model) == ("system_objects", PolicyQuerySet)
    assert _base(Opted) == ("_rebac_base", TrackedQuerySet)


@pytest.mark.parametrize("manager_class", [models.Manager, RebacManager])
def test_declared_base_manager_without_tracking_is_refused(manager_class):
    with isolate_apps("tests.testapp"), pytest.raises(ImproperlyConfigured) as raised:

        class Revision(RebacMixin, models.Model):
            system_objects = manager_class()

            class Meta:
                app_label = "testapp"
                rebac_resource_type = "test/revision"
                base_manager_name = "system_objects"

    message = str(raised.value)
    assert "testapp.Revision" in message
    assert "'system_objects'" in message
    assert "TrackedQuerySet" in message


@isolate_apps("tests.testapp")
def test_untracked_base_manager_of_a_parent_is_refused_on_the_child():
    class Legacy(models.Model):
        everything = models.Manager.from_queryset(PlainQuerySet)()

        class Meta:
            abstract = True
            base_manager_name = "everything"

    with pytest.raises(ImproperlyConfigured, match="'everything'"):

        class Revision(RebacMixin, Legacy):
            class Meta:
                app_label = "testapp"
                rebac_resource_type = "test/revision"


@isolate_apps("tests.testapp")
def test_undeclared_base_manager_is_the_injected_one():
    class Plain(RebacMixin, models.Model):
        class Meta:
            app_label = "testapp"
            rebac_resource_type = "test/plain"

    class Tracked(RebacTrackedMixin):
        class Meta:
            app_label = "testapp"

    for model in (Plain, Tracked, Post, Folder):
        assert _base(model) == ("_rebac_base", TrackedQuerySet)
        assert type(model._base_manager) is TrackedManager
        assert model._meta.default_manager_name == "objects"
    assert isinstance(Post._default_manager, RebacManager)


@pytest.mark.parametrize(
    ("manager_class", "fragment"),
    [
        (HidingManager, "filters rows"),
        (UntrackedManager, "returns PlainQuerySet"),
    ],
)
def test_check_reports_a_base_manager_that_hides_rows_or_drops_tracking(manager_class, fragment):
    with isolate_apps("tests.testapp") as apps:

        class Revision(RebacMixin, models.Model):
            hidden = models.BooleanField(default=False)
            system_objects = manager_class()

            class Meta:
                app_label = "testapp"
                rebac_resource_type = "test/revision"
                base_manager_name = "system_objects"

        issues = check_declared_base_managers(app_configs=apps.get_app_configs())

    assert [issue.id for issue in issues] == ["rebac.E023"]
    assert isinstance(issues[0], checks.Error)
    assert issues[0].obj is Revision
    assert fragment in issues[0].msg


def test_check_accepts_the_injected_and_the_declared_tracked_base_managers():
    assert check_declared_base_managers() == []


@pytest.fixture
def installed(db):
    local = install_schema(parse_zed(SCHEMA))
    with sudo(reason="declared base manager tests"):
        yield local


def _readable(local, row):
    return local.check_access(subject=ALICE, action="read", resource=to_object_ref(row)).allowed


def test_install_schema_makes_the_backend_current_over_existing_rows(db):
    first = install_schema("definition auth/user {}")
    assert backend() is first
    with sudo(reason="rows written before their schema is installed"):
        folder = Folder.objects.create(name="shared")
        post = DeclaredBasePost.objects.create(title="early", folder=folder)
    explicit = LocalBackend()

    assert install_schema(parse_zed(SCHEMA), backend=explicit) is explicit
    assert backend() is explicit
    assert not _readable(explicit, post)
    backend().write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", ALICE)])
    assert _readable(explicit, post)


def test_install_schema_parses_schema_text(db):
    local = install_schema(SCHEMA)

    assert backend() is local
    assert {d.resource_type for d in local.schema().definitions} >= {"test/declaredbasepost"}


def test_writes_through_a_declared_base_manager_change_reads(installed):
    assert DeclaredBasePost._base_manager._queryset_class is OwnerQuerySet
    shared = Folder.objects.create(name="shared")
    private = Folder.objects.create(name="private")
    installed.write_relationships([RelationshipTuple(to_object_ref(shared), "viewer", ALICE)])

    created, moved = DeclaredBasePost._base_manager.owner_bulk_create(
        [
            DeclaredBasePost(title="created", folder=shared),
            DeclaredBasePost(title="moved", folder=private),
        ]
    )
    assert _readable(installed, created)
    assert not _readable(installed, moved)

    DeclaredBasePost._base_manager.filter(pk=moved.pk).update(folder=shared)
    assert _readable(installed, moved)

    created.folder = private
    DeclaredBasePost._base_manager.bulk_update([created], ["folder"])
    assert not _readable(installed, created)


@pytest.mark.parametrize("operation", ["update", "bulk_update"])
def test_writes_through_a_declared_base_manager_are_gated(db, operation):
    local = install_schema(GATED_SCHEMA)
    with sudo(reason="declared base manager fixture"):
        shared = Folder.objects.create(name="shared")
        private = Folder.objects.create(name="private")
        post = DeclaredBasePost.objects.create(title="post", folder=private)
    local.write_relationships([RelationshipTuple(to_object_ref(shared), "viewer", ALICE)])

    def move():
        if operation == "update":
            DeclaredBasePost._base_manager.filter(pk=post.pk).update(folder=shared)
        else:
            post.folder = shared
            DeclaredBasePost._base_manager.bulk_update([post], ["folder"])

    with pytest.raises(MissingActorError):
        move()
    with actor_context(ALICE), pytest.raises(PermissionDenied):
        move()
    assert DeclaredBasePost._base_manager.get(pk=post.pk).folder_id == private.pk
    assert not _readable(local, post)

    local.write_relationships([RelationshipTuple(to_object_ref(post), "editor", ALICE)])
    with actor_context(ALICE):
        move()
    assert DeclaredBasePost._base_manager.get(pk=post.pk).folder_id == shared.pk
    assert _readable(local, post)


def test_django_reaches_a_declared_base_manager_and_keeps_its_policy(installed):
    shared = Folder.objects.create(name="shared")
    installed.write_relationships([RelationshipTuple(to_object_ref(shared), "viewer", ALICE)])
    post = DeclaredBasePost.objects.create(title="post", folder=shared)
    assert _readable(installed, post)

    with pytest.raises(TypeError, match="never deleted in bulk"):
        DeclaredBasePost._base_manager.filter(pk=post.pk).delete()

    shared.delete()  # the collector's SET_NULL runs through the declared base manager
    post.refresh_from_db()
    assert post.folder_id is None
    assert not _readable(installed, post)
