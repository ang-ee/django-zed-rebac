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
    permission_sources,
    permissions_reaching_relation,
    relation_dependencies,
)
from .parser import ParseError, parse_permission_expression, parse_zed, validate_schema

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
    "parse_permission_expression",
    "parse_zed",
    "permission_sources",
    "permissions_reaching_relation",
    "relation_dependencies",
    "validate_schema",
]
