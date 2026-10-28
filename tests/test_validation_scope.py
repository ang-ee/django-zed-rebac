"""Django validation compares hidden rows only after the native save gate admits it."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models

from rebac import RebacMixin, actor_context, backend, current_actor, system_context
from rebac.backends import reset_backend
from rebac.errors import MissingActorError, PermissionDenied
from rebac.models import PermissionAuditEvent
from rebac.schema import parse_zed
from tests.backend_setup import install_schema

pytestmark = pytest.mark.django_db


class GuardedUniqueRow(RebacMixin):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    email = models.CharField(max_length=80, unique=True)
    code = models.CharField(max_length=40, unique=True, null=True, blank=True)
    title = models.CharField(max_length=40, default="row")

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "validation/unique_row"


class OwnerDefaultUniqueRow(RebacMixin):
    """A consumer model owns its insert default; the engine must evaluate that save."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="+",
    )
    code = models.CharField(max_length=40, unique=True)

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "validation/default_row"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self._state.adding and self.owner_id is None and (actor := current_actor()) is not None:
            self.owner_id = actor.subject_id
        super().save(*args, **kwargs)


class GuardedConstraintRow(RebacMixin):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    code = models.CharField(max_length=40)

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "validation/constraint_row"
        constraints = [models.UniqueConstraint(fields=("code",), name="validation_code_unique")]


class PlainValidationRow(RebacMixin):
    title = models.CharField(max_length=40)

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "validation/plain_row"


SCHEMA = """
definition auth/user {}
definition validation/unique_row {
    relation owner: auth/user // rebac:field=owner
    relation keeper: auth/user
    permission read = owner + keeper
    permission create = owner
    permission write = owner + keeper
    permission delete = owner
    permission read__email = keeper
    permission write__email = keeper
    permission read__code = owner + keeper
    permission write__code = keeper
}
definition validation/default_row {
    relation owner: auth/user // rebac:field=owner
    permission read = owner
    permission create = owner
    permission write = owner
}
definition validation/constraint_row {
    relation owner: auth/user // rebac:field=owner
    permission read = owner
    permission create = owner
    permission write = owner
}
definition validation/plain_row {
    permission read = authenticated
    permission create = authenticated
    permission write = authenticated
}
"""


@pytest.fixture(params=("denormalized", "registry"))
def validation_rows(request: pytest.FixtureRequest, settings: Any) -> Iterator[SimpleNamespace]:
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA))
    users = get_user_model().objects
    alice = users.create_user(username="validation-alice")
    bob = users.create_user(username="validation-bob")
    with system_context(reason="validation fixture"):
        mine = GuardedUniqueRow.objects.create(owner=alice, email="alice@example.com", code="A1")
        GuardedUniqueRow.objects.create(owner=bob, email="bob@example.com", code="B1")
        GuardedConstraintRow.objects.create(owner=bob, code="taken")
    try:
        yield SimpleNamespace(alice=alice, bob=bob, mine=mine)
    finally:
        reset_backend()


def test_unreadable_unwritten_values_cannot_reveal_hidden_duplicates(validation_rows):
    with actor_context(validation_rows.alice):
        row = GuardedUniqueRow.objects.get(pk=validation_rows.mine.pk)
        row.email = "bob@example.com"
        row.full_clean()
        row.save()
    with system_context(reason="validation readback"):
        assert GuardedUniqueRow.objects.get(pk=row.pk).email == "alice@example.com"


def test_denied_write_does_not_answer_whether_a_readable_value_is_taken(validation_rows):
    with actor_context(validation_rows.alice):
        row = GuardedUniqueRow.objects.get(pk=validation_rows.mine.pk)
        row.code = "B1"
        row.full_clean(exclude=["email"])
        with pytest.raises(PermissionDenied):
            row.save()


def test_admitted_create_validates_hidden_unique_rows(validation_rows):
    with actor_context(validation_rows.alice), pytest.raises(ValidationError) as caught:
        GuardedUniqueRow(owner=validation_rows.alice, email="bob@example.com").full_clean()
    assert set(caught.value.message_dict) == {"email"}


def test_admitted_create_validates_native_unique_constraints(validation_rows):
    with actor_context(validation_rows.alice), pytest.raises(ValidationError) as caught:
        GuardedConstraintRow(owner=validation_rows.alice, code="taken").full_clean()
    assert set(caught.value.message_dict) == {"code"}


def test_unevaluable_candidate_keeps_its_own_field_errors(validation_rows):
    with actor_context(validation_rows.alice), pytest.raises(ValidationError) as caught:
        GuardedUniqueRow(owner_id=999_999, email="bob@example.com").full_clean()
    assert set(caught.value.message_dict) == {"owner"}


def test_system_and_instance_bypasses_validate_against_every_row(validation_rows):
    with system_context(reason="validation ambient bypass"):
        before = PermissionAuditEvent.objects.count()
        with pytest.raises(ValidationError):
            GuardedUniqueRow(owner=validation_rows.alice, email="bob@example.com").full_clean()
        assert PermissionAuditEvent.objects.count() == before
    with actor_context(validation_rows.alice), pytest.raises(ValidationError):
        GuardedUniqueRow(owner=validation_rows.alice, email="bob@example.com").sudo(
            reason="validation instance bypass",
        ).full_clean()


def test_actorless_validation_retains_strict_native_reads(validation_rows):
    with pytest.raises(MissingActorError):
        GuardedUniqueRow(owner=validation_rows.alice, email="bob@example.com").full_clean()


def test_steps_without_cross_row_rules_do_not_elevate(validation_rows):
    with actor_context(validation_rows.alice):
        before = PermissionAuditEvent.objects.count()
        PlainValidationRow(title="plain").full_clean()
        assert PermissionAuditEvent.objects.count() == before
        GuardedUniqueRow(owner=validation_rows.alice, email="new@example.com").full_clean()
        assert PermissionAuditEvent.objects.count() == before + 1


def test_owning_save_default_is_used_then_restored_during_validation(validation_rows):
    with system_context(reason="validation owner default fixture"):
        OwnerDefaultUniqueRow.objects.create(owner=validation_rows.alice, code="taken")
    with actor_context(validation_rows.alice):
        row = OwnerDefaultUniqueRow(code="taken")
        with pytest.raises(ValidationError) as caught:
            row.full_clean()
        assert set(caught.value.message_dict) == {"code"}
        assert row.owner_id is None
        row.code = "free"
        row.full_clean()
        row.save()
    assert str(row.owner_id) == str(validation_rows.alice.pk)


def test_save_verdict_is_not_kept_between_validation_calls(validation_rows):
    row = GuardedUniqueRow(owner=validation_rows.bob, email="bob@example.com")
    with actor_context(validation_rows.alice):
        row.full_clean()
        with pytest.raises(PermissionDenied):
            row.save()
        row.owner = validation_rows.alice
        with pytest.raises(ValidationError) as caught:
            row.full_clean()
    assert set(caught.value.message_dict) == {"email"}


@pytest.mark.parametrize(
    "model,duplicate,available",
    [
        (GuardedUniqueRow, {"email": "bob@example.com"}, {"email": "fresh@example.com"}),
        (GuardedConstraintRow, {"code": "taken"}, {"code": "fresh"}),
    ],
)
def test_nested_consumer_save_validation_preserves_outer_permission_decision(
    validation_rows, monkeypatch, model, duplicate, available,
):
    """A save that calls full_clean cannot hide a duplicate from outer validation."""

    native_save = model.save

    def validating_save(instance, *args, **kwargs):
        instance.full_clean()
        return native_save(instance, *args, **kwargs)

    # This test owns these probe models; the override is the consumer lifecycle
    # check_save must execute, rather than a substitute for native validation.
    monkeypatch.setattr(model, "save", validating_save)
    with system_context(reason="nested validation count"):
        before = model._base_manager.count()
    rejected = model(owner=validation_rows.alice, **duplicate)
    with actor_context(validation_rows.alice), pytest.raises(ValidationError):
        rejected.full_clean()
    assert rejected.pk is None
    assert "_rebac_save_verdict" not in rejected.__dict__

    accepted = model(owner=validation_rows.alice, **available)
    with actor_context(validation_rows.alice):
        accepted.full_clean()
    assert accepted.pk is None
    assert "_rebac_save_verdict" not in accepted.__dict__
    with system_context(reason="nested validation count"):
        assert model._base_manager.count() == before
