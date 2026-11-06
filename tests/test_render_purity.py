"""Rendering a permission scope never reads the database.

The queryset owner prepares a scope when it builds it: the policy and the
caveat verdicts are pinned then. Compiling the predicate into SQL, the first
time or any later time, alone or as an operand of a combined query, executes
no statement. The schema here is stored in the database and no evaluator scope
is open, so a policy read at render would be observable.

Four named exceptions do database work at render, outside a prepared scope:

- (a) ``permission_expression`` reads the policy and the caveat-verdict rows
  when its SQL is compiled, by design: its executing database and policy bind
  at compilation.
- (b) The field-read guard reads the schema revision when an actor-scoped
  subquery renders inside a plain outer query and no evaluator scope is open.
- (c) The embedded-bypass audit writes its row at render for a statement whose
  query class REBAC does not hook: Django's insert query, the delete query of
  a collector fast delete, and a plain Django queryset's statement whose REBAC
  subquery Django resolves only while compiling it.
- (d) A REBAC queryset that Django resolves only while compiling the outer
  statement (a ``Subquery`` in ``ORDER BY``, a REBAC ``Subquery`` as an INSERT
  value) builds its scope then: it reads the schema revision, unless an
  evaluator scope pins it, and the caveat-verdict rows.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.models import Exists, OuterRef
from django.test.utils import CaptureQueriesContext

from rebac import LocalBackend, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.backends import reset_backend
from rebac.compile import read
from rebac.models import SchemaDefinition, SchemaPermission, SchemaRelation
from tests.testapp.models import Post

pytestmark = pytest.mark.django_db
ACTOR = SubjectRef.of("auth/user", "render-purity")


@pytest.fixture
def stored_policy():
    SchemaDefinition.objects.create(resource_type="auth/user")
    definition = SchemaDefinition.objects.create(resource_type="blog/post")
    SchemaRelation.objects.create(
        definition=definition, name="viewer", allowed_subjects=[{"type": "auth/user"}]
    )
    SchemaPermission.objects.create(definition=definition, name="read", expression="viewer")
    reset_backend()
    local = LocalBackend()
    with sudo(reason="render purity fixture"):
        post = Post.objects.create(title="visible")
    local.write_relationships([RelationshipTuple(to_object_ref(post), "viewer", ACTOR)])
    yield local, post
    reset_backend()


def _scope(local):
    return read.scope_q(backend=local, model=Post, action="read", actor=ACTOR, using="default")


@pytest.mark.parametrize("shape", ["filter", "exists", "union_operand", "candidates"])
def test_first_render_of_a_prepared_scope_executes_nothing(stored_policy, shape):
    local, post = stored_policy
    scope = _scope(local)
    rows = Post._base_manager.filter(scope)
    if shape == "exists":
        statement = Post._base_manager.annotate(
            allowed=Exists(rows.filter(pk=OuterRef("pk")))
        ).values("pk", "allowed")
    elif shape == "union_operand":
        # A combinator operand reaches the compiler without its queryset owner.
        statement = rows.values("pk").union(Post._base_manager.filter(_scope(local)).values("pk"))
    elif shape == "candidates":
        statement = rows.filter(pk__in=[post.pk])
    else:
        statement = rows
    for _render in range(2):
        with CaptureQueriesContext(connection) as queries:
            assert "SELECT" in str(statement.query)
        assert len(queries) == 0
    assert post.pk in set(statement.values_list("pk", flat=True))
