"""Pins for the private Django query-resolution behaviours an embedded queryset relies on.

A queryset embedded in a statement is decided again by each statement that compiles it,
for another actor, by resolving its already resolved query into the statement a second
time (``_RebacQuery.as_sql``), from conditions of its own (``_own_conditions``). That is
correct only while Django resolves a resolved query the way pinned here, on plain Django
models with no REBAC involved. A change to any of these needs a review of the embedding
mechanism before the Django pin is widened (ARCHITECTURE.md, Versioning and support).
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.db.models import Exists, OuterRef, Subquery
from django.db.models.expressions import Col
from django.db.models.lookups import Lookup

pytestmark = pytest.mark.django_db


def _embedded() -> tuple[object, object]:
    """A statement with a correlated subquery, and the subquery as resolved into it."""
    inner = Group.objects.filter(name=OuterRef("username")).values("pk")[:1]
    outer = User.objects.annotate(group=Subquery(inner))
    # Django keeps the resolved query itself as the annotation.
    resolved = outer.query.annotations["group"]
    return outer.query, getattr(resolved, "query", resolved)


def test_a_resolved_query_resolves_again_to_the_same_statement():
    outer, resolved = _embedded()
    again = resolved.resolve_expression(outer, allow_joins=True, reuse=None)
    assert again is not resolved
    assert str(again) == str(resolved)
    # The aliases were moved off the outer statement's once: bump_prefix returns early
    # when the prefixes already differ.
    assert again.alias_prefix == resolved.alias_prefix != outer.alias_prefix
    assert again.subquery


def test_an_outer_reference_is_a_column_of_the_outer_statement_once_resolved():
    outer, resolved = _embedded()
    [lookup] = resolved.where.children
    assert isinstance(lookup.rhs, Col)
    assert lookup.rhs.alias == outer.get_initial_alias()
    again = lookup.rhs.resolve_expression(outer)
    assert isinstance(again, Col)
    assert (again.alias, again.target) == (lookup.rhs.alias, lookup.rhs.target)


def test_resolving_a_where_tree_assigns_the_sides_of_its_lookups_in_place():
    """WhereNode.resolve_expression clones the nodes, not the lookups, and assigns each
    lookup's sides; Query.resolve_expression discards the clone it resolves and keeps
    the lookups' new sides. An embedded queryset is therefore resolved from copies of
    its conditions, or resolving it writes on the statement it was copied from."""
    inner = Group.objects.filter(name=OuterRef("username")).values("pk")
    [lookup] = inner.query.where.children
    assert isinstance(lookup, Lookup)
    before = lookup.rhs
    outer = User.objects.all().query
    inner.query.where.resolve_expression(outer, allow_joins=True, reuse=None)
    assert lookup.rhs is not before
    clone = inner.query.clone()
    assert clone.where.children[0] is lookup


def test_a_condition_is_resolved_again_where_its_statement_compiles():
    """Lookup.process_lhs resolves the left-hand side again into the compiling statement:
    the precedent the embedding mechanism follows."""
    rows = User.objects.filter(Exists(Group.objects.filter(name=OuterRef("username"))))
    [lookup] = rows.query.where.children
    resolved: list[object] = []
    query_class = type(lookup.lhs.query)
    original = query_class.resolve_expression

    def counted(self, *args, **kwargs):
        resolved.append(self)
        return original(self, *args, **kwargs)

    query_class.resolve_expression = counted
    try:
        str(rows.query)
    finally:
        query_class.resolve_expression = original
    assert resolved
