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
