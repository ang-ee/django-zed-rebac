"""``REBAC_TRUSTED_IDS`` (proposal 0020): stored ids of an integer identity converted by the cast alone."""

from __future__ import annotations

import pytest
from django.db import DatabaseError, connection, transaction
from django.test.utils import CaptureQueriesContext

from rebac.compile import read
from rebac.testing import install_schema
from tests.mechanism_policies import ADMIN, MEMBER, application_shaped, grant, label_threads
from tests.test_actor_profiles import seeded
from tests.test_bounded_walk import SCHEMA
from tests.test_bounded_walk import local as local
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
GUARD = "[^0-9-]"  # the validity guard's second pattern, in the statement text


def rows(model, actor=MEMBER):
    with CaptureQueriesContext(connection) as captured:
        found = sorted(model.objects.with_actor(actor).values_list("pk", flat=True))
    return found, [query["sql"] for query in captured.captured_queries]


def trust(settings, value: bool) -> None:
    settings.REBAC_TRUSTED_IDS = value
    read.reset()  # kept statements carry the conversion they were compiled with


def test_the_conversion_is_the_cast_alone(local, settings):
    local.write_relationships([grant("scope/thread", 1, "reader")])
    expected, guarded = rows(ScopePart)
    assert expected == [1, 2] and any(GUARD in sql for sql in guarded)
    trust(settings, True)
    try:
        found, trusted = rows(ScopePart)
        assert found == expected
        assert not any(GUARD in sql for sql in trusted)
        assert sum(map(len, trusted)) < sum(map(len, guarded))
    finally:
        trust(settings, False)


@pytest.mark.django_db(transaction=True)
def test_an_id_that_is_not_canonical_is_passed_over_only_by_the_guard(local, settings):
    """The contract of the setting: every stored id of an integer identity is canonical."""
    backend = install_schema(SCHEMA)
    backend.write_relationships(
        [grant("scope/thread", 1, "reader"), grant("scope/thread", "x", "reader")]
    )
    assert rows(ScopeThread)[0] == [1]
    trust(settings, True)
    try:
        if connection.vendor == "postgresql":
            with pytest.raises(DatabaseError), transaction.atomic():
                rows(ScopeThread)
        else:
            # SQLite reads the text by its numeric value: "x" is 0, no row.
            assert rows(ScopeThread)[0] == [1]
    finally:
        trust(settings, False)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("seed", range(20))
def test_rows_read_are_the_same_on_canonical_ids(seed, storage, settings):
    schema, tuples, labels = application_shaped(seed)
    seeded(schema, tuples, storage, settings)
    label_threads(labels)
    for actor in (ADMIN, MEMBER):
        for model in (ScopeThread, ScopeMessage, ScopePart):
            trust(settings, False)
            expected, _ = rows(model, actor)
            trust(settings, True)
            try:
                assert rows(model, actor)[0] == expected, (actor, model.__name__, seed)
            finally:
                trust(settings, False)
