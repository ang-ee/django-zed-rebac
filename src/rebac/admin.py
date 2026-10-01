"""Django admin registrations for the REBAC plugin.

Scope per ARCHITECTURE.md § SchemaOverride:
  - SchemaOverride: full writable form; save_model stamps created_by.
  - Tier-1 schema models: read-only — managed via `rebac sync`, not admin.
  - PermissionAuditEvent + PackageManagedRecord: read-only system rows.
  - Relationship: read-only; writes go through the Backend API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import (
    PackageManagedRecord,
    PermissionAuditEvent,
    Relationship,
    SchemaCaveat,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
)

if TYPE_CHECKING:
    from django.forms import ModelForm


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


# ---------------------------------------------------------------------------
# Shared base classes
# ---------------------------------------------------------------------------


class _ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class _ReadOnlyTabularInline(admin.TabularInline):  # type: ignore[type-arg]
    extra = 0

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


# ---------------------------------------------------------------------------
# Tier-2: SchemaOverride — writable admin (required per spec)
# ---------------------------------------------------------------------------


@admin.register(SchemaOverride)
class SchemaOverrideAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "kind",
        "target_label",
        "expression_summary",
        "reason_summary",
        "created_by",
        "created_at",
        "expires_at",
    )
    list_filter = ("kind", "created_at")
    list_select_related = ["target_ct", "created_by"]
    search_fields = ("expression", "reason")
    readonly_fields = ("created_by", "created_at")
    date_hierarchy = "created_at"
    fieldsets = (
        (
            None,
            {"fields": ("kind", "target_ct", "target_pk", "expression")},
        ),
        (
            "Reason & expiry",
            {"fields": ("reason", "expires_at")},
        ),
        (
            "Provenance",
            {
                "fields": ("created_by", "created_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[SchemaOverride]:
        return super().get_queryset(request).select_related("target_ct", "created_by")

    @admin.display(description="Target")
    def target_label(self, obj: SchemaOverride) -> str:
        target = obj.target  # GenericForeignKey returns None when referent is missing
        if target is None:
            return f"{obj.target_ct.app_label}.{obj.target_ct.model}/{obj.target_pk}"
        return str(target)

    @admin.display(description="Expression")
    def expression_summary(self, obj: SchemaOverride) -> str:
        return _truncate(obj.expression, 80)

    @admin.display(description="Reason")
    def reason_summary(self, obj: SchemaOverride) -> str:
        return _truncate(obj.reason, 60)

    def save_model(
        self,
        request: HttpRequest,
        obj: SchemaOverride,
        form: ModelForm[SchemaOverride],
        change: bool,
    ) -> None:
        if not change and request.user.is_authenticated:
            obj.created_by = request.user  # narrowed to AbstractBaseUser by is_authenticated guard
        super().save_model(request, obj, form, change)


# ---------------------------------------------------------------------------
# Tier-3: Relationship — read-only (writes go through Backend API)
# ---------------------------------------------------------------------------


@admin.register(Relationship)
class RelationshipAdmin(_ReadOnlyAdmin):
    list_display = (
        "resource_type",
        "resource_id",
        "relation",
        "subject_type",
        "subject_id",
        "optional_subject_relation",
        "expires_at",
    )
    list_filter = ("resource_type", "relation", "subject_type")
    search_fields = ("resource_id", "subject_id", "caveat_name")
    # expires_at is mostly NULL — date_hierarchy would hide most rows


# ---------------------------------------------------------------------------
# Audit: PermissionAuditEvent — append-only, read-only
# ---------------------------------------------------------------------------


@admin.register(PermissionAuditEvent)
class PermissionAuditEventAdmin(_ReadOnlyAdmin):
    list_display = ("kind", "actor_summary", "target_repr", "reason_summary", "occurred_at")
    list_filter = ("kind", "occurred_at")
    search_fields = ("actor_subject_id", "target_repr", "reason")
    date_hierarchy = "occurred_at"
    readonly_fields = (
        "kind",
        "actor_subject_type",
        "actor_subject_id",
        "target_repr",
        "before",
        "after",
        "reason",
        "occurred_at",
    )

    def get_actions(self, request: HttpRequest) -> dict[str, Any]:
        # Belt-and-braces: remove bulk delete from an append-only table.
        return {}

    @admin.display(description="Actor")
    def actor_summary(self, obj: PermissionAuditEvent) -> str:
        if obj.actor_subject_type:
            return f"{obj.actor_subject_type}:{obj.actor_subject_id}"
        return "<system>"

    @admin.display(description="Reason")
    def reason_summary(self, obj: PermissionAuditEvent) -> str:
        return _truncate(obj.reason, 60)


# ---------------------------------------------------------------------------
# Tier-1: Schema models — read-only (managed via `rebac sync`)
# ---------------------------------------------------------------------------


class SchemaRelationInline(_ReadOnlyTabularInline):
    model = SchemaRelation
    fields = ("name", "allowed_subjects", "caveat", "with_expiration")
    readonly_fields = ["name", "allowed_subjects", "caveat", "with_expiration"]


class SchemaPermissionInline(_ReadOnlyTabularInline):
    model = SchemaPermission
    fields = ("name", "expression")
    readonly_fields = ["name", "expression"]


@admin.register(SchemaDefinition)
class SchemaDefinitionAdmin(_ReadOnlyAdmin):
    list_display = ("resource_type",)
    search_fields = ("resource_type",)
    inlines = [SchemaRelationInline, SchemaPermissionInline]


@admin.register(SchemaCaveat)
class SchemaCaveatAdmin(_ReadOnlyAdmin):
    list_display = ("name", "expression_summary")
    search_fields = ("name",)
    readonly_fields = ("name", "params", "expression")

    @admin.display(description="Expression")
    def expression_summary(self, obj: SchemaCaveat) -> str:
        return _truncate(obj.expression, 80)


# ---------------------------------------------------------------------------
# Provenance: PackageManagedRecord — read-only
# ---------------------------------------------------------------------------


@admin.register(PackageManagedRecord)
class PackageManagedRecordAdmin(_ReadOnlyAdmin):
    list_display = ("package", "external_id", "schema_revision", "no_update", "last_synced_at")
    list_filter = ("package", "no_update")
    search_fields = ("package", "external_id")
    date_hierarchy = "last_synced_at"
