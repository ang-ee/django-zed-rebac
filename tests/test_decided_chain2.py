import pytest

from rebac import SubjectRef, to_object_ref
from rebac.compile import read
from tests.test_recursive_queryscope import chain, grant, no_enumeration, schema_context
from tests.testapp.models import Folder

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]


@pytest.mark.parametrize("variant", ["list-only", "with-check", "with-alias", "full"])
def test_chain_steps(variant):
    with schema_context("denormalized", "folder", "field") as (active, member, hop, action):
        rows = chain(active, hop, "field", 1)
        actors = [SubjectRef.of("auth/user", str(100 + i)) for i in range(len(rows))]
        for row, actor in zip(rows, actors, strict=True):
            grant(active, row, member, actor)
        out = []
        with no_enumeration(active):
            for actor in actors:
                got = sorted(
                    Folder.objects.with_actor(actor)
                    .with_action(action)
                    .values_list("pk", flat=True)
                )
                out.append(("list", actor.subject_id, got, read.statement_cache_stats()["hits"]))
                if variant in ("with-check", "full"):
                    for row in rows:
                        active.check_access(
                            subject=actor, action=action, resource=to_object_ref(row)
                        )
                if variant in ("with-alias", "full"):
                    got = sorted(
                        Folder.objects.with_actor(actor)
                        .with_action("alias")
                        .values_list("pk", flat=True)
                    )
                    out.append(
                        ("alias", actor.subject_id, got, read.statement_cache_stats()["hits"])
                    )
