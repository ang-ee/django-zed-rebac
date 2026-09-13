"""Django models.

Tables: ``Relationship`` + ``RelationshipRegistry`` + ``RebacResource`` +
``Schema*`` (4) + ``PackageManagedRecord`` + ``SchemaOverride``
+ ``PermissionAuditEvent``. Per ARCHITECTURE.md § Models.

Both ``Relationship`` and ``RelationshipRegistry`` ship in the migration; only
one is *active* per process, selected by ``REBAC_LOCAL_BACKEND_STORAGE`` via
:func:`active_relationship_model`. Engine code routes through that helper.
"""

from __future__ import annotations

from .audit import PermissionAuditEvent
from .overrides import SchemaOverride
from .provenance import PackageManagedRecord
from .relationship import (
    Relationship,
    RelationshipManager,
    RelationshipRegistry,
    RelationshipRegistryManager,
    active_relationship_model,
)
from .resource import RebacResource
from .schema import (
    SchemaCaveat,
    SchemaDefinition,
    SchemaPermission,
    SchemaRelation,
)

RelationshipRow = Relationship | RelationshipRegistry
"""A row of whichever relationship storage model is active."""

__all__ = [
    "PackageManagedRecord",
    "PermissionAuditEvent",
    "RebacResource",
    "Relationship",
    "RelationshipManager",
    "RelationshipRegistry",
    "RelationshipRegistryManager",
    "RelationshipRow",
    "SchemaCaveat",
    "SchemaDefinition",
    "SchemaOverride",
    "SchemaPermission",
    "SchemaRelation",
    "active_relationship_model",
]
