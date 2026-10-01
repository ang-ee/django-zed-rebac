"""Index read SQL is constant across recursive graph depths."""

import re
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from rebac import RelationshipTuple, to_object_ref
from rebac.schema import parse_zed
from tests.backend_setup import STORAGE_TIERS, install_schema
from tests.test_recursive_queryscope import (
    ACTOR,
    OUTSIDER,
    chain,
    grant,
    no_enumeration,
    recursive_schema,
    schema_context,
)
from tests.testapp.models import Folder

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


def deep_scope_proof(storage, backing, deep):
    measurements = []
    with schema_context(storage, "folder", backing) as (active, member, hop, action):
        for depth in (1, deep):
            rows = chain(active, hop, backing, depth, prefix=str(depth))
            grant(active, rows[0], member)
            assert active.check_access(
                subject=ACTOR, action=action, resource=to_object_ref(rows[-1])
            ).allowed
            queryset = (
                Folder.objects.with_actor(ACTOR).with_action(action).filter(pk=rows[-1].pk).scoped()
            )
            statements = []

            def record(execute, sql, params, many, context, statements=statements):
                statements.append(sql)
                return execute(sql, params, many, context)

            with connection.execute_wrapper(record):
                sql, params = queryset.query.sql_with_params()
                # Compiling the scope reads the actor's stored sets, once.
                assert len(statements) == 1
                assert list(queryset.values_list("pk", flat=True)) == [rows[-1].pk]
            # Evaluating it reads them again, then the rows.
            assert len(statements) == 3
            measurements.append((len(sql), len(params), parse_depth(sql)))
            for subject, expected in ((ACTOR, False), (OUTSIDER, True)):
                assert (
                    Folder.objects.with_actor(subject)
                    .with_action("exclusion")
                    .filter(pk=rows[-1].pk)
                    .exists()
                    is expected
                )
    assert measurements[0] == measurements[1]
    return measurements


@pytest.mark.pg_delta
@pytest.mark.parametrize("storage", STORAGE_TIERS)
@pytest.mark.parametrize("backing", ["tuple", "field"])
@pytest.mark.parametrize("deep", [3, pytest.param(8, marks=pytest.mark.slow)])
def test_deep_scopes_have_constant_parse_depth(storage, backing, deep):
    deep_scope_proof(storage, backing, deep)


@pytest.mark.parametrize("storage", STORAGE_TIERS)
@pytest.mark.parametrize("backing", ["tuple", "field", "path"])
def test_recursive_exclusions_keep_expiry_and_intermediate_denials(storage, backing):
    with (
        schema_context(storage, "folder", backing) as (active, member, hop, action),
    ):
        text, *_ = recursive_schema("folder", backing)
        text = "use expiration\n" + text.replace(
            "permission read = (reader) + parent->read",
            "relation blocked: auth/user with expiration\n"
            "permission inherited = reader + parent->inherited\n"
            "permission read = inherited - blocked",
        )
        install_schema(active, parse_zed(text))
        rows = chain(active, hop, backing, 3)
        Folder.objects.sudo(reason="backing fixture update").filter(
            pk__in=[row.pk for row in rows]
        ).update(is_active=True)
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
