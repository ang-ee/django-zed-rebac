"""Canonical Zed rendering of the native schema AST."""

import json

from .ast import (
    AllowedSubject,
    AttributeBinding,
    Caveat,
    ConstBinding,
    Definition,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermNil,
    PermRef,
    Relation,
    Schema,
    backing_to_dict,
)


def render_zed(schema: Schema, *, include_backing: bool = True) -> str:
    """Render deterministic source preserving all semantic AST data.

    Source comments/whitespace are not retained by the parser. Declaration and
    subject ordering is canonical; operand order and CEL are preserved.
    ``include_backing=False`` emits the tuple-only SpiceDB export form.
    """
    lines = [f"// @{key}: {value}" for key, value in sorted(schema.headers.items())]
    lines.extend(schema.directives)
    blocks = [_render_caveat(caveat) for caveat in sorted(schema.caveats, key=lambda c: c.name)]
    blocks.extend(
        _render_definition(definition, include_backing=include_backing)
        for definition in sorted(schema.definitions, key=lambda d: d.resource_type)
    )
    if lines and blocks:
        lines.append("")
    return "\n".join([*lines, *blocks]).rstrip() + "\n"


def _render_caveat(caveat: Caveat) -> str:
    params = ", ".join(f"{param.name} {param.type}" for param in caveat.params)
    return f"caveat {caveat.name}({params}) {{\n{caveat.expression}\n}}\n"


def _render_definition(definition: Definition, *, include_backing: bool) -> str:
    relations = sorted(definition.relations, key=lambda r: r.name)
    permissions = sorted(definition.permissions, key=lambda p: p.name)
    lines = [f"definition {definition.resource_type} {{"]
    for relation in relations:
        lines.append(f"    {_render_relation(relation, include_backing=include_backing)}")
    if relations and permissions:
        lines.append("")
    for permission in permissions:
        lines.append(f"    permission {permission.name} = {_render_expr(permission.expression)}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _render_relation(relation: Relation, *, include_backing: bool) -> str:
    subjects = sorted(
        relation.allowed_subjects,
        key=lambda s: (s.type, s.id, s.relation, s.wildcard, s.with_caveat),
    )
    rendered = " | ".join(render_allowed_subject(subject) for subject in subjects)
    suffix = " with expiration" if relation.with_expiration else ""
    line = f"relation {relation.name}: {rendered}{suffix}"
    backing = relation.backing
    if backing is None or not include_backing:
        return line
    if isinstance(backing, ConstBinding):
        return f"{line} // rebac:const={backing.target_id}"
    if isinstance(backing, FieldBinding):
        if not backing.filters:
            return f"{line} // rebac:field={backing.attname}"
        data = backing_to_dict(backing)
        assert data is not None
        data.pop("kind")
        data["path"] = data.pop("attname")
        return f"{line} // rebac:field={json.dumps(data, sort_keys=True, separators=(',', ':'))}"
    if isinstance(backing, AttributeBinding):
        data = backing_to_dict(backing)
        assert data is not None
        data.pop("kind")
        return f"{line} // rebac:attribute={json.dumps(data, sort_keys=True, separators=(',', ':'))}"
    raise TypeError(f"{relation.name}: unsupported relation backing kind {backing.kind!r}")


def render_allowed_subject(subject: AllowedSubject) -> str:
    """Render one allowed subject, preserving identity, subject set and caveat."""
    if subject.wildcard:
        base = f"{subject.type}:*"
    elif subject.id and subject.relation:
        base = f"{subject.type}:{subject.id}#{subject.relation}"
    elif subject.id:
        base = f"{subject.type}:{subject.id}"
    elif subject.relation:
        base = f"{subject.type}#{subject.relation}"
    else:
        base = subject.type
    if subject.with_caveat:
        base += f" with {subject.with_caveat}"
    return base


def _render_expr(expr: PermExpr) -> str:
    if isinstance(expr, PermNil):
        return "nil"
    if isinstance(expr, PermRef):
        return expr.name
    if isinstance(expr, PermArrow):
        return f"{expr.via}->{expr.target}"
    if isinstance(expr, PermBinOp):
        return f"({_render_expr(expr.left)} {expr.op} {_render_expr(expr.right)})"
    raise TypeError(f"unknown expression node: {type(expr).__name__}")
