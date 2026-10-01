"""A model with no ``Meta`` of its own takes its parent's Django options, as in Django."""

from django.db import models
from django.test.utils import isolate_apps

from rebac import RebacManager, RebacMixin, RebacTrackedMixin, TrackedQuerySet


def _options(model):
    meta = model._meta
    return (
        list(meta.ordering),
        [index.name for index in meta.indexes],
        [constraint.name for constraint in meta.constraints],
        meta.verbose_name_plural,
    )


@isolate_apps("tests.testapp")
def test_child_without_meta_takes_the_django_options_of_its_abstract_rebac_parent():
    class Audited(RebacTrackedMixin):
        name = models.CharField(max_length=50)

        class Meta:
            abstract = True
            app_label = "testapp"
            ordering = ("name",)
            verbose_name_plural = "audited things"
            indexes = (models.Index(fields=["name"], name="%(class)s_name_idx"),)
            constraints = (models.UniqueConstraint(fields=["name"], name="%(class)s_name_uniq"),)

    class Tag(Audited):
        pass

    class Label(Tag):
        class Meta(Audited.Meta):
            pass

    assert Tag._meta.app_label == "testapp"
    assert _options(Tag) == (["name"], ["tag_name_idx"], ["tag_name_uniq"], "audited things")
    assert _options(Label)[0] == ["name"]
    assert not Tag._meta.abstract
    assert Tag._meta.base_manager_name == "_rebac_base"
    assert Tag._base_manager._queryset_class is TrackedQuerySet
    assert Tag._meta.default_manager_name == "objects"


@isolate_apps("tests.testapp")
def test_child_without_meta_takes_the_meta_of_its_first_base_that_has_one():
    class Named(models.Model):
        name = models.CharField(max_length=50)

        class Meta:
            abstract = True
            app_label = "testapp"
            ordering = ("name",)

    class ParentFirst(Named, RebacTrackedMixin):
        pass

    class MixinFirst(RebacTrackedMixin, Named):
        class Meta:
            app_label = "testapp"

    assert ParentFirst._meta.ordering == ("name",)
    assert ParentFirst._meta.base_manager_name == "_rebac_base"
    assert ParentFirst._meta.default_manager_name == "objects"
    # Django's rule: a Meta of its own that does not subclass the parent's inherits nothing.
    assert MixinFirst._meta.ordering == []


@isolate_apps("tests.testapp")
def test_rebac_options_are_carried_only_by_a_meta_the_class_writes():
    class Identified(RebacMixin, models.Model):
        public_id = models.CharField(max_length=64, unique=True)

        class Meta:
            abstract = True
            app_label = "testapp"
            ordering = ("public_id",)
            rebac_resource_type = "test/identified"
            rebac_id_attr = "public_id"

    class Bare(Identified):
        pass

    class Explicit(Identified):
        class Meta(Identified.Meta):
            rebac_resource_type = "test/explicit"

    assert Bare._meta.ordering == ("public_id",)
    assert not hasattr(Bare._meta, "rebac_resource_type")
    assert not hasattr(Bare._meta, "rebac_id_attr")
    assert isinstance(Bare._default_manager, RebacManager)
    assert Explicit._meta.ordering == ("public_id",)
    assert Explicit._meta.rebac_resource_type == "test/explicit"
    assert Explicit._meta.rebac_id_attr == "public_id"


@isolate_apps("tests.testapp")
def test_child_without_meta_keeps_a_parents_manager_names():
    class Policy(RebacTrackedMixin):
        everything = models.Manager.from_queryset(TrackedQuerySet)()

        class Meta:
            abstract = True
            app_label = "testapp"
            base_manager_name = "everything"
            default_manager_name = "everything"

    class Row(Policy):
        pass

    assert Row._meta.base_manager_name == "everything"
    assert Row._meta.default_manager_name == "everything"
    assert Row._default_manager.name == "everything"
