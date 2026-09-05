"""SpiceDB-native .zed schema parser + AST + compiler."""

from __future__ import annotations

from .ast import (
    AllowedSubject,
    Caveat,
    CaveatParam,
    ConstBinding,
    Definition,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    Permission,
    PermNil,
    PermRef,
    Relation,
    Schema,
)
from .introspection import (
    PermissionSources,
    permission_object_sources,
    permission_sources,
    permissions_reaching_relation,
    relation_dependencies,
)
from .parser import ParseError, parse_permission_expression, parse_zed, validate_schema
from .rendering import render_allowed_subject, render_zed
from .sources import resolve_schema_path

__all__ = [
    "AllowedSubject",
    "Caveat",
    "CaveatParam",
    "ConstBinding",
    "Definition",
    "FieldBinding",
    "ParseError",
    "PermArrow",
    "PermBinOp",
    "PermExpr",
    "PermNil",
    "PermRef",
    "Permission",
    "PermissionSources",
    "Relation",
    "Schema",
    "parse_permission_expression",
    "parse_zed",
    "permission_object_sources",
    "permission_sources",
    "permissions_reaching_relation",
    "relation_dependencies",
    "render_allowed_subject",
    "render_zed",
    "resolve_schema_path",
    "validate_schema",
]
