"""``rebac.admin`` — ARCHITECTURE.md § SchemaOverride and the module's scope note.

``SchemaOverride`` has a writable admin that stamps ``created_by``; relationship,
audit, provenance and Tier-1 schema admins are read-only, even for superusers.

``tests.settings`` does not install ``django.contrib.admin``. The ModelAdmin
classes are exercised on a private ``AdminSite`` with ``RequestFactory``
requests: changelists, permissions, forms and ``save_model`` need neither the
admin app nor a URLconf. Importing ``rebac.admin`` currently needs a runtime
generic shim; see ``test_admin_module_loads_in_a_project_with_the_admin_app``.
"""

from __future__ import annotations

import importlib
import io
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from django.contrib.admin import AdminSite
from django.contrib.admin.options import BaseModelAdmin
from django.contrib.admin.utils import lookup_field
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import RequestFactory

from rebac import ObjectRef, RelationshipTuple, SubjectRef, actor_context, write_relationships
from rebac.models import (
    PackageManagedRecord,
    PermissionAuditEvent,
    Relationship,
    SchemaCaveat,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
)
from tests.backend_setup import atomic_source_write

REPO_ROOT = Path(__file__).resolve().parents[1]

READ_ONLY_MODELS = [
    Relationship,
    PermissionAuditEvent,
    SchemaDefinition,
    SchemaCaveat,
    PackageManagedRecord,
]


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> AdminSite:
    """Load ``rebac.admin`` against a private site, leaving no process state behind.

    ``@admin.register`` resolves ``django.contrib.admin.sites.site`` when it runs,
    so the registrations land on ``private``. The module subscripts ModelAdmin
    classes, which Django does not support at runtime; the shim exists only
    while the module body executes.
    """
    private = AdminSite(name="rebac_test_admin")
    monkeypatch.setattr("django.contrib.admin.sites.site", private)
    monkeypatch.delitem(sys.modules, "rebac.admin", raising=False)
    shimmed = "__class_getitem__" not in BaseModelAdmin.__dict__
    if shimmed:
        BaseModelAdmin.__class_getitem__ = classmethod(lambda cls, *args: cls)  # type: ignore[attr-defined]
    try:
        importlib.import_module("rebac.admin")
    finally:
        if shimmed:
            del BaseModelAdmin.__class_getitem__  # type: ignore[attr-defined]
        sys.modules.pop("rebac.admin", None)
    return private


@pytest.fixture
def superuser(db):
    return atomic_source_write(
        get_user_model().objects.create_superuser,
        username="root",
        email="root@example.com",
        password=None,
    )


@pytest.fixture
def request_as(superuser):
    def build(user: Any = None, path: str = "/", data: dict[str, str] | None = None) -> Any:
        request = RequestFactory().get(path, data or {})
        request.user = user or superuser
        return request

    return build


@pytest.fixture
def seeded(db, superuser, django_capture_on_commit_callbacks) -> SchemaOverride:
    """One or more rows in every admin-registered table."""
    with django_capture_on_commit_callbacks(execute=True):
        call_command("rebac", "sync", stdout=io.StringIO())
        SchemaCaveat.objects.create(
            name="weekday", params=[{"name": "day", "type": "int"}], expression="day < 6"
        )
        write_relationships(
            [
                RelationshipTuple(
                    resource=ObjectRef("blog/post", "1"),
                    relation="owner",
                    subject=SubjectRef.of("auth/user", str(superuser.pk)),
                )
            ]
        )
        permission = SchemaPermission.objects.get(
            definition__resource_type="blog/post", name="read"
        )
        override = SchemaOverride.objects.create(
            kind=SchemaOverride.KIND_TIGHTEN,
            target_ct=ContentType.objects.get_for_model(SchemaPermission),
            target_pk=permission.pk,
            expression="owner",
            reason="owners only while under review",
        )
        PermissionAuditEvent.objects.create(kind=PermissionAuditEvent.KIND_SUDO_BYPASS)
    return override


def test_admin_module_registers_the_documented_models(site: AdminSite) -> None:
    assert set(site._registry) == {SchemaOverride, *READ_ONLY_MODELS}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "src/rebac/admin.py:42,53,72 subscript admin.ModelAdmin/TabularInline at runtime; "
        "Django 6.0 classes are not generic without django_stubs_ext.monkeypatch(), so "
        "admin autodiscovery raises TypeError in any project that installs the admin."
    ),
)
def test_admin_module_loads_in_a_project_with_the_admin_app() -> None:
    script = """
import django
from django.conf import settings

settings.configure(
    SECRET_KEY="x",
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    INSTALLED_APPS=[
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.messages",
        "rebac",
    ],
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
)
django.setup()
from django.contrib import admin
from rebac.models import SchemaOverride

assert admin.site.is_registered(SchemaOverride)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "model", [SchemaOverride, *READ_ONLY_MODELS], ids=lambda model: model.__name__
)
def test_changelist_loads_every_row(site: AdminSite, seeded, request_as, model) -> None:
    model_admin = site._registry[model]
    changelist = model_admin.get_changelist_instance(request_as())
    expected = model._default_manager.count()
    assert expected > 0
    assert changelist.result_count == expected
    rows = list(changelist.result_list)
    assert len(rows) == expected
    for row in rows:
        for name in model_admin.get_list_display(request_as()):
            if name != "action_checkbox":
                lookup_field(name, row, model_admin)


def test_changelist_search_narrows_rows(site: AdminSite, seeded, request_as) -> None:
    model_admin = site._registry[SchemaDefinition]
    changelist = model_admin.get_changelist_instance(request_as(data={"q": "blog/post"}))
    assert [row.resource_type for row in changelist.result_list] == ["blog/post"]


@pytest.mark.parametrize("model", READ_ONLY_MODELS, ids=lambda model: model.__name__)
def test_read_only_admins_deny_writes_to_superusers(
    site: AdminSite, seeded, request_as, model
) -> None:
    model_admin = site._registry[model]
    request = request_as()
    row = model._default_manager.first()
    assert request.user.is_superuser
    assert model_admin.has_view_permission(request, row)
    for obj in (None, row):
        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request, obj)
        assert not model_admin.has_delete_permission(request, obj)
    assert model_admin.get_actions(request) == {}


def test_schema_definition_inlines_are_read_only(site: AdminSite, seeded, request_as) -> None:
    model_admin = site._registry[SchemaDefinition]
    request = request_as()
    definition = SchemaDefinition.objects.get(resource_type="blog/post")
    inlines = model_admin.get_inline_instances(request, definition)
    assert {type(inline).__name__ for inline in inlines} == {
        "SchemaRelationInline",
        "SchemaPermissionInline",
    }
    for inline in inlines:
        assert not inline.has_add_permission(request, definition)
        assert not inline.has_change_permission(request, definition)
        assert not inline.has_delete_permission(request, definition)


def test_audit_admin_has_no_bulk_delete_action(site: AdminSite, seeded, request_as) -> None:
    model_admin = site._registry[PermissionAuditEvent]
    # The site offers bulk delete; the append-only audit table removes it.
    assert "delete_selected" in dict(site.actions)
    assert model_admin.get_actions(request_as()) == {}


def test_schema_override_admin_allows_superuser_writes(site: AdminSite, seeded, request_as) -> None:
    model_admin = site._registry[SchemaOverride]
    request = request_as()
    assert model_admin.has_add_permission(request)
    assert model_admin.has_change_permission(request, seeded)
    assert model_admin.has_delete_permission(request, seeded)


def test_schema_override_add_sets_created_by_and_audits(
    site: AdminSite, seeded, superuser, request_as, django_capture_on_commit_callbacks
) -> None:
    model_admin = site._registry[SchemaOverride]
    request = request_as()
    target = SchemaPermission.objects.get(definition__resource_type="blog/post", name="write")
    form_class = model_admin.get_form(request)
    assert "created_by" not in form_class.base_fields
    form = form_class(
        data={
            "kind": SchemaOverride.KIND_DISABLE,
            "target_ct": str(ContentType.objects.get_for_model(SchemaPermission).pk),
            "target_pk": str(target.pk),
            "expression": "owner",
            "reason": "freeze writes during the audit",
        }
    )
    assert form.is_valid(), form.errors
    override = form.save(commit=False)
    PermissionAuditEvent.objects.all().delete()

    # ActorMiddleware scopes the admin request to its user.
    with (
        django_capture_on_commit_callbacks(execute=True),
        actor_context(SubjectRef.of("auth/user", str(superuser.pk))),
    ):
        model_admin.save_model(request, override, form, change=False)

    override.refresh_from_db()
    assert override.created_by == superuser
    event = PermissionAuditEvent.objects.get(kind=PermissionAuditEvent.KIND_OVERRIDE_CREATE)
    assert event.reason == "freeze writes during the audit"
    assert event.target_repr == f"disable:rebac.schemapermission/{target.pk}"
    assert event.after == {
        "kind": "disable",
        "expression": "owner",
        "reason": "freeze writes during the audit",
    }
    assert (event.actor_subject_type, event.actor_subject_id) == ("auth/user", str(superuser.pk))


def test_schema_override_change_keeps_created_by(
    site: AdminSite, seeded, superuser, request_as
) -> None:
    model_admin = site._registry[SchemaOverride]
    seeded.created_by = superuser
    seeded.save()
    editor = atomic_source_write(
        get_user_model().objects.create_superuser,
        username="editor",
        email="editor@example.com",
        password=None,
    )
    seeded.reason = "edited"
    form = model_admin.get_form(request_as(editor), seeded, change=True)(instance=seeded)
    model_admin.save_model(request_as(editor), seeded, form, change=True)
    seeded.refresh_from_db()
    assert seeded.created_by == superuser
    assert seeded.reason == "edited"


def test_override_target_label_with_present_and_missing_referent(site: AdminSite, seeded) -> None:
    model_admin = site._registry[SchemaOverride]
    assert model_admin.target_label(seeded) == "blog/post#read"
    dangling = SchemaOverride(
        kind=SchemaOverride.KIND_LOOSEN,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=987654,
        expression="owner",
        reason="x" * 100,
    )
    assert model_admin.target_label(dangling) == "rebac.schemapermission/987654"
    assert model_admin.reason_summary(dangling) == "x" * 60 + "…"
    assert model_admin.expression_summary(dangling) == "owner"


def test_audit_labels_distinguish_system_rows(site: AdminSite, db) -> None:
    model_admin = site._registry[PermissionAuditEvent]
    system = PermissionAuditEvent(kind=PermissionAuditEvent.KIND_SCHEMA_SYNC, reason="r" * 61)
    actor = PermissionAuditEvent(
        kind=PermissionAuditEvent.KIND_SUDO_BYPASS,
        actor_subject_type="auth/user",
        actor_subject_id="7",
        reason="short",
    )
    assert model_admin.actor_summary(system) == "<system>"
    assert model_admin.actor_summary(actor) == "auth/user:7"
    assert model_admin.reason_summary(system) == "r" * 60 + "…"
    assert model_admin.reason_summary(actor) == "short"


def test_caveat_expression_summary_truncates(site: AdminSite, db) -> None:
    model_admin = site._registry[SchemaCaveat]
    long_expression = " || ".join(f"day == {n}" for n in range(20))
    caveat = SchemaCaveat(name="long", expression=long_expression)
    assert model_admin.expression_summary(caveat) == long_expression[:80] + "…"
