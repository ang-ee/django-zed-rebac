"""A second actor sees only its own recursive field-chain rows."""

import pytest
from django.db import connection

from rebac import SubjectRef
from rebac.compile import read
from tests.test_recursive_queryscope import chain, grant, schema_context

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


@pytest.mark.parametrize("shape", ["role", "folder"])
def test_chain_second_actor(shape):
    with schema_context("denormalized", shape, "field") as (active, member, hop, action):
        rows = chain(active, hop, "field", 1)
        a0, a1 = SubjectRef.of("auth/user", "100"), SubjectRef.of("auth/user", "101")
        grant(active, rows[0], member, a0)
        grant(active, rows[1], member, a1)
        from tests.testapp.models import Folder

        read.reset()
        first = sorted(
            Folder.objects.with_actor(a0).with_action(action).values_list("pk", flat=True)
        )
        kept = [v for v in read._statements.values() if not isinstance(v, bool)]
        literals = [
            p for v in kept for p in v.params if p in (rows[0].pk, str(rows[0].pk), [rows[0].pk])
        ]
        second = sorted(
            Folder.objects.with_actor(a1).with_action(action).values_list("pk", flat=True)
        )
        read.reset()
        cold = sorted(
            Folder.objects.with_actor(a1).with_action(action).values_list("pk", flat=True)
        )
        rec = {
            "probe": "chain",
            "shape": shape,
            "vendor": connection.vendor,
            "rows": [r.pk for r in rows],
            "actor100": first,
            "actor101_warm": second,
            "actor101_cold": cold,
            "literal_row0_params_in_kept_sql": len(literals),
        }
        assert second == cold == [rows[1].pk], rec
