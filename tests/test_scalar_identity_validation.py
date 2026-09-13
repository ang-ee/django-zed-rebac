"""Live relation identities must map one wire id to one query expression."""

from __future__ import annotations

import pytest
from django.db import models
from django.test.utils import isolate_apps

from rebac.field_backing import _validate_model_identity


@isolate_apps("tests")
def test_composite_primary_key_is_not_a_scalar_live_backing_identity() -> None:
    class CompositeResource(models.Model):
        tenant_id = models.IntegerField()
        local_id = models.IntegerField()
        pk = models.CompositePrimaryKey("tenant_id", "local_id")

        class Meta:
            app_label = "tests"

    with pytest.raises(ValueError, match="identity 'pk' must be a scalar field"):
        _validate_model_identity(CompositeResource, "pk")


@isolate_apps("tests")
def test_mti_parent_link_pk_and_scalar_attname_are_queryable_identities() -> None:
    class Parent(models.Model):
        class Meta:
            app_label = "tests"

    class Child(Parent):
        class Meta:
            app_label = "tests"

    parent_link = Child._meta.pk
    assert parent_link.remote_field.parent_link

    _validate_model_identity(Child, "pk")
    _validate_model_identity(Child, parent_link.attname)


@isolate_apps("tests")
def test_relation_descriptor_is_not_a_scalar_identity_but_its_attname_is() -> None:
    class Target(models.Model):
        class Meta:
            app_label = "tests"

    class Resource(models.Model):
        target = models.ForeignKey(Target, on_delete=models.CASCADE)

        class Meta:
            app_label = "tests"

    _validate_model_identity(Resource, "target_id")
    with pytest.raises(ValueError, match="identity 'target' must be a scalar field"):
        _validate_model_identity(Resource, "target")
