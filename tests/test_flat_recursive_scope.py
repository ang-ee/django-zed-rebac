"""Portable recursive SQL: depth widens joins, not the parser stack."""

import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from rebac import PermissionDepthExceeded, RelationshipTuple, to_object_ref
from rebac.backends.local_query import LocalQueryScope
from rebac.schema import parse_zed
from tests.test_queryset_permission_parity import SCHEMA
from tests.test_recursive_queryscope import (
    ACTOR,
    OUTSIDER,
    chain,
    grant,
    no_enumeration,
    recursive_schema,
    schema_context,
)
from tests.testapp.models import AuthoredPost, Folder, Post

pytestmark = pytest.mark.django_db


def parse_depth(sql):
    """Measure SELECT nesting and parentheses, ignoring quoted SQL tokens."""
    stack = [False]
    selects = maximum_selects = maximum_parens = 0
    for match in re.finditer(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\bSELECT\b|[()]", sql):
        token = match.group()
        if token == "(":
            stack.append(False)
            maximum_parens = max(maximum_parens, len(stack) - 1)
        elif token == ")":
            selects -= stack.pop()
        elif token == "SELECT":
            stack[-1] = True
            selects += 1
            maximum_selects = max(maximum_selects, selects)
    assert len(stack) == 1
    return maximum_selects, maximum_parens


def deep_scope_proof(storage, backing):
    measurements = []
    statements = []

    def record(execute, sql, params, many, context):
        statements.append(sql)
        return execute(sql, params, many, context)

    with schema_context(storage, "folder", backing) as (active, member, hop, action):
        rows = chain(active, hop, backing, 33)
        grant(active, rows[0], member)
        for bound in (8, 24, 32):
            statements.clear()
            with override_settings(REBAC_DEPTH_LIMIT=bound), no_enumeration(active):
                assert active.check_access(
                    subject=ACTOR, action=action, resource=to_object_ref(rows[bound])
                ).allowed
                predicate = LocalQueryScope(active, ACTOR, "default").predicate(
                    Folder, action, "blog/folder"
                )
                queryset = Folder._base_manager.filter(predicate, pk=rows[bound].pk).values("pk")
                with connection.execute_wrapper(record):
                    sql, params = queryset.query.sql_with_params()
                    with connection.cursor() as cursor:
                        cursor.execute(sql, params)
                        assert cursor.fetchall() == [(rows[bound].pk,)]
                assert len(statements) == 2  # Deferred frontier and the scoped read.
                depths = tuple(parse_depth(statement) for statement in statements)
                measurements.append((bound, depths, len(sql)))
                for subject, expected in ((ACTOR, False), (OUTSIDER, True)):
                    assert (
                        Folder.objects.with_actor(subject)
                        .with_action("exclusion")
                        .filter(pk=rows[bound].pk)
                        .exists()
                    ) is expected
                with pytest.raises(PermissionDepthExceeded) as check_error:
                    active.check_access(
                        subject=ACTOR, action=action, resource=to_object_ref(rows[bound + 1])
                    )
                with pytest.raises(PermissionDepthExceeded) as scope_error:
                    Folder.objects.with_actor(ACTOR).filter(pk=rows[bound + 1].pk).exists()
                assert (
                    str(check_error.value)
                    == str(scope_error.value)
                    == (f"Depth limit {bound} exceeded")
                )
    assert len({depths for _, depths, _ in measurements}) == 1, measurements
    return measurements


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("backing", ["tuple", "field"])
def test_deep_scopes_have_constant_parse_depth(storage, backing):
    deep_scope_proof(storage, backing)


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("backing", ["tuple", "field", "path"])
def test_recursive_exclusions_keep_expiry_and_intermediate_denials(storage, backing):
    with (
        override_settings(REBAC_DEPTH_LIMIT=3),
        schema_context(storage, "folder", backing) as (active, member, hop, action),
    ):
        text, *_ = recursive_schema("folder", backing)
        text = "use expiration\n" + text.replace(
            "permission read = (reader) + parent->read",
            "relation blocked: auth/user with expiration\n"
            "permission read = (reader + parent->read) - blocked",
        )
        active.set_schema(parse_zed(text))
        rows = chain(active, hop, backing, 3)
        Folder._base_manager.filter(pk__in=[row.pk for row in rows]).update(is_active=True)
        grant(active, rows[0], member)
        grant(active, rows[2], member)
        active.write_relationships(
            [
                RelationshipTuple(
                    to_object_ref(rows[1]),
                    "blocked",
                    ACTOR,
                    expires_at=timezone.now() + timedelta(days=1),
                ),
                RelationshipTuple(
                    to_object_ref(rows[2]),
                    "blocked",
                    ACTOR,
                    expires_at=timezone.now() - timedelta(days=1),
                ),
            ]
        )
        expected = {rows[index].pk for index in (0, 2, 3)}
        with no_enumeration(active):
            assert set(Folder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == expected
            for row in rows:
                assert active.check_access(
                    subject=ACTOR, action=action, resource=to_object_ref(row)
                ).allowed is (row.pk in expected)


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_acyclic_sql_without_group_definition_keeps_original_compiler(storage):
    # The test application's newly declared group must not obscure the acyclic
    # contract. This schema deliberately omits that definition; the existing
    # ten SHA-256 fingerprints also retain their non-recursive group definition.
    with schema_context(storage, "folder", "tuple") as (active, *_):
        active.set_schema(
            parse_zed(
                SCHEMA.replace("definition auth/group {\n    relation member: auth/user\n}\n", "")
            )
        )
        with patch(
            "rebac.backends.local_recursive.FlatPredicate", side_effect=AssertionError("flattened")
        ):
            for model, actions in (
                (Post, ("union_read", "intersection_read", "read", "inherited_read")),
                (AuthoredPost, ("read",)),
            ):
                for action in actions:
                    predicate = LocalQueryScope(active, ACTOR, "default").predicate(
                        model, action, model._meta.rebac_resource_type
                    )
                    model._base_manager.filter(predicate).query.sql_with_params()
