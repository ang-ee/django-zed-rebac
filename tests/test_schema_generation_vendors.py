"""Opt in with REBAC_TEST_SCHEMA_VENDORS=1 pytest -m schema_vendors.

Exercise Django schema owners and upgrades on disposable PostgreSQL/MySQL containers;
Docker storage is ephemeral and containers are removed even after failure.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

pytestmark = [
    pytest.mark.schema_vendors,
    pytest.mark.skipif(
        os.environ.get("REBAC_TEST_SCHEMA_VENDORS") != "1",
        reason="set REBAC_TEST_SCHEMA_VENDORS=1 to run disposable Docker database tests",
    ),
]


def _run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=120, **kwargs)


@pytest.fixture(params=["postgresql", "mysql"])
def vendor_database(request):
    vendor = request.param
    mysql = vendor == "mysql"
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    container = f"rebac-schema-test-{os.getpid()}-{worker}-{vendor}"
    image = "mysql:8.0" if mysql else "postgres:16"
    data = "/var/lib/mysql" if mysql else "/var/lib/postgresql/data"
    secret = "MYSQL_ROOT_PASSWORD=schema-test" if mysql else "POSTGRES_PASSWORD=schema-test"
    started = _run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container,
            "--tmpfs",
            data,
            *([] if mysql else ["--memory", "768m", "--memory-swap", "768m"]),
            "-p",
            "127.0.0.1::" + ("3306" if mysql else "5432"),
            "-e",
            secret,
            "-e",
            ("MYSQL_DATABASE" if mysql else "POSTGRES_DB") + "=rebac_schema",
            image,
        ]
    )
    assert started.returncode == 0, started.stderr
    try:
        ready = (
            ["mysql", "-h127.0.0.1", "-uroot", "-pschema-test", "-e", "SELECT 1"]
            if mysql
            else ["pg_isready", "-U", "postgres"]
        )
        for _ in range(60):
            if _run(["docker", "exec", container, *ready]).returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"{vendor} did not become ready")

        port = _run(["docker", "port", container, "3306" if mysql else "5432"])
        assert port.returncode == 0, port.stderr
        yield vendor, container, port.stdout.strip().rsplit(":", 1)[-1]
    finally:
        _run(["docker", "rm", "-f", container])


@pytest.fixture
def vendor_connection(vendor_database, django_db_blocker):
    vendor, _container, port = vendor_database
    pytest.importorskip("MySQLdb" if vendor == "mysql" else "psycopg")

    from django.db import connections
    from django.db.utils import ConnectionHandler

    alias = f"schema_owner_{vendor}"
    handler = ConnectionHandler(
        {
            "default": {
                "ENGINE": "django.db.backends." + vendor,
                "NAME": "rebac_schema",
                "USER": "root" if vendor == "mysql" else "postgres",
                "PASSWORD": "schema-test",
                "HOST": "127.0.0.1",
                "PORT": port,
            }
        }
    )
    db = handler["default"]
    db.alias = alias
    connections[alias] = db
    try:
        with django_db_blocker.unblock():
            yield db
    finally:
        with django_db_blocker.unblock():
            db.close()
        del connections[alias]


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.django_db(transaction=True)
def test_vendor_django_schema_owners_and_upgrade(vendor_connection, settings, storage):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    exercise_vendor_owners(vendor_connection)


@pytest.mark.parametrize("vendor_database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.django_db(transaction=True)
def test_postgresql_additive_scope(vendor_connection, monkeypatch, storage):
    from django.core.management import call_command
    from django.test import override_settings

    from .test_scope_additive import exercise_binding_scope

    db = vendor_connection

    class Router:
        def db_for_read(self, model, **hints):
            return db.alias

        def db_for_write(self, model, **hints):
            return db.alias

    call_command("migrate", database=db.alias, verbosity=0)
    with db.cursor() as cursor:
        cursor.execute("SET jit = off")
        cursor.execute("SET statement_timeout = '5s'")
    with override_settings(DATABASE_ROUTERS=[Router()]):
        exercise_binding_scope(db, monkeypatch, storage, measure_probe=True)


def exercise_vendor_owners(db):
    """Also callable by a disposable runner with the vendor's native driver."""
    from django.core.management import call_command
    from django.db import transaction

    from rebac import LocalBackend
    from rebac.models.generation import SchemaGeneration

    from .test_schema_write_owners import (
        installed_database_objects,
        policy_row,
        upgrade_schema_owners,
    )

    local = LocalBackend()
    assert local._read_schema_revision(db) is None
    with transaction.atomic(using=db.alias):
        assert local._read_schema_revision(db) is None
        with db.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone()[0] == 1
    call_command("migrate", database=db.alias, verbosity=0)
    upgrade_schema_owners(db)
    for kind in ("definition", "relation", "permission", "caveat", "override"):
        # Roll back fixtures too, keeping each model scenario independent.
        with transaction.atomic(using=db.alias):
            row, changes = policy_row(kind, using=db.alias)
            manager = type(row).objects.using(db.alias)
            revisions = [local._read_schema_revision(db)]
            manager.bulk_create([row])
            if row.pk is None:  # MySQL does not return IDs from bulk inserts.
                row = manager.latest("pk")
            revisions.append(local._read_schema_revision(db))
            manager.filter(pk=row.pk).update(**changes)
            revisions.append(local._read_schema_revision(db))
            for field, value in changes.items():
                setattr(row, field, value)
            manager.bulk_update([row], list(changes))
            revisions.append(local._read_schema_revision(db))
            row.save(using=db.alias)
            revisions.append(local._read_schema_revision(db))
            with transaction.atomic(using=db.alias):
                manager.filter(pk=row.pk).delete()
                assert local._read_schema_revision(db) not in revisions
                transaction.set_rollback(True, using=db.alias)
            assert local._read_schema_revision(db) == revisions[-1]
            row.delete(using=db.alias)
            revisions.append(local._read_schema_revision(db))
            assert len(set(revisions)) == len(revisions)
            transaction.set_rollback(True, using=db.alias)
    assert installed_database_objects(db) == set()
    SchemaGeneration.objects.using(db.alias).all().delete()
    assert local._read_schema_revision(db) is None
    SchemaGeneration.objects.advance(using=db.alias)
    assert local._read_schema_revision(db) is not None
