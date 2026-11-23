"""Structural delivery admits a parent on the database it was read from (proposal 0020).

Threads read from one database say nothing of the messages of another: the children of
a prefetch read from another database keep their permission statement, which reads that
database's relationships.
"""

from __future__ import annotations

import pytest
from django.db import connections
from django.db.models import Prefetch

from rebac import ObjectRef, RelationshipTuple, SubjectRef
from rebac.testing import install_schema
from tests.backend_setup import sqlite_alias
from tests.testapp.models import ScopeMessage, ScopeThread

ALICE = SubjectRef.of("auth/user", "alice")
SCHEMA = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    permission read = thread->read
}
"""


@pytest.mark.django_db
def test_an_admission_is_kept_with_its_database():
    from rebac import structural

    install_schema(SCHEMA)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    [thread] = ScopeThread._base_manager.filter(pk=1)
    query = ScopeMessage.objects.filter(thread_id__in=[1]).query
    with structural.delivering(ALICE, ScopeThread, "read", [thread]):
        assert structural.delivers(query, ScopeMessage, ALICE, "read", using="default")
        assert not structural.delivers(query, ScopeMessage, ALICE, "read", using="elsewhere")


@pytest.mark.django_db
def test_parents_from_one_database_deliver_no_children_from_another(
    settings, django_db_blocker, tmp_path
):
    from django.core.management import call_command

    alias = "structural_other"
    target = sqlite_alias(alias, tmp_path / "other.sqlite3")
    connections[alias] = target
    try:
        with django_db_blocker.unblock():
            call_command("migrate", database=alias, verbosity=0)
            local = install_schema(SCHEMA)
            ScopeThread._base_manager.using("default").bulk_create([ScopeThread(id=1)])
            ScopeThread._base_manager.using(alias).bulk_create([ScopeThread(id=1)])
            ScopeMessage._base_manager.using(alias).bulk_create([ScopeMessage(id=7, thread_id=1)])
            # Alice reads thread 1 on the default database; the other one holds no grant.
            local.write_relationships(
                [RelationshipTuple(ObjectRef("scope/thread", "1"), "reader", ALICE)]
            )
            assert not ScopeMessage.objects.using(alias).with_actor(ALICE).exists()

            def read(child_database: str):
                threads = (
                    ScopeThread.objects.using("default")
                    .with_actor(ALICE)
                    .prefetch_related(
                        Prefetch(
                            "scopemessage_set",
                            queryset=ScopeMessage.objects.using(child_database).with_actor(ALICE),
                        )
                    )
                )
                return {t.pk: sorted(m.pk for m in t.scopemessage_set.all()) for t in threads}

            settings.REBAC_STRUCTURAL_PREFETCH = False
            scoped = read(alias)
            settings.REBAC_STRUCTURAL_PREFETCH = True
            assert read(alias) == scoped == {1: []}
    finally:
        target.close()
        del connections[alias]


@pytest.mark.django_db
def test_parents_from_another_database_deliver_no_children_from_the_default_one(
    settings, django_db_blocker, tmp_path
):
    """The other direction: the parents are read from another database, which holds the
    grant, and the children from the default one, which holds none. An admission recorded
    under any database but its own would deliver them."""
    from django.core.management import call_command

    from rebac.models import active_relationship_model

    alias = "structural_parents"
    target = sqlite_alias(alias, tmp_path / "parents.sqlite3")
    connections[alias] = target
    try:
        with django_db_blocker.unblock():
            call_command("migrate", database=alias, verbosity=0)
            local = install_schema(SCHEMA)
            for database in ("default", alias):
                ScopeThread._base_manager.using(database).bulk_create([ScopeThread(id=1)])
            ScopeMessage._base_manager.using("default").bulk_create(
                [ScopeMessage(id=7, thread_id=1)]
            )
            # Alice's grant, written through the library, moved to the other database.
            local.write_relationships(
                [RelationshipTuple(ObjectRef("scope/thread", "1"), "reader", ALICE)]
            )
            relationships = active_relationship_model()._base_manager
            rows = list(relationships.using("default").all())
            for row in rows:
                row.pk = None
            relationships.using(alias).bulk_create(rows)
            relationships.using("default").all().delete()
            assert ScopeThread.objects.using(alias).with_actor(ALICE).exists()
            assert not ScopeMessage.objects.using("default").with_actor(ALICE).exists()

            def read() -> dict[int, list[int]]:
                threads = (
                    ScopeThread.objects.using(alias)
                    .with_actor(ALICE)
                    .prefetch_related(
                        Prefetch(
                            "scopemessage_set",
                            queryset=ScopeMessage.objects.using("default").with_actor(ALICE),
                        )
                    )
                )
                return {t.pk: sorted(m.pk for m in t.scopemessage_set.all()) for t in threads}

            settings.REBAC_STRUCTURAL_PREFETCH = False
            scoped = read()
            settings.REBAC_STRUCTURAL_PREFETCH = True
            assert read() == scoped == {1: []}
    finally:
        target.close()
        del connections[alias]
