"""Canonical model resource identity validation."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from django.utils.functional import SimpleLazyObject

from rebac import ObjectRef, to_object_ref
from rebac.resources import model_resource_id


class BoundModel:
    _meta = SimpleNamespace(
        rebac_resource_type="test/resource",
        rebac_id_attr="public_id",
    )

    def __init__(self, public_id: object) -> None:
        self.public_id = public_id


@pytest.mark.parametrize("value", [None, ""])
def test_model_resource_id_rejects_empty_identity(value: object) -> None:
    resource = BoundModel(value)

    with pytest.raises(TypeError, match="rebac_id_attr='public_id' resolved to an empty value"):
        model_resource_id(resource)
    with pytest.raises(TypeError, match="rebac_id_attr='public_id' resolved to an empty value"):
        to_object_ref(resource)


@pytest.mark.parametrize(
    ("value", "wire_id"),
    [
        (0, "0"),
        (UUID("12345678-1234-5678-1234-567812345678"), "12345678-1234-5678-1234-567812345678"),
    ],
)
def test_model_resource_id_preserves_valid_scalar_identity(value: object, wire_id: str) -> None:
    resource = BoundModel(value)

    assert model_resource_id(resource) == wire_id
    assert to_object_ref(resource) == ObjectRef("test/resource", wire_id)


@pytest.mark.django_db
def test_lazy_django_model_uses_wrapped_resource_metadata() -> None:
    from rebac import sudo
    from tests.testapp.models import SubjectContainer

    with sudo(reason="lazy resource fixture"):
        subject = SubjectContainer.objects.create(slug="reviewers", title="Reviewers")
    lazy_subject = SimpleLazyObject(
        lambda: SubjectContainer.objects.sudo(reason="resolve lazy resource").get(pk=subject.pk)
    )

    assert to_object_ref(lazy_subject) == ObjectRef("blog/subjectcontainer", "reviewers")
