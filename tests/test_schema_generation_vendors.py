"""Opt in with REBAC_TEST_SCHEMA_VENDORS=1 pytest -m schema_vendors.

Exercise the migration's actual SQL on disposable PostgreSQL/MySQL containers;
Docker storage is ephemeral and containers are removed even after failure.
"""

from __future__ import annotations

import os
import subprocess
import time
from importlib import import_module
from types import SimpleNamespace

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
    container = f"rebac-schema-test-{os.getpid()}-{vendor}"
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
            "-p",
            "127.0.0.1::" + ("3306" if mysql else "5432"),
            "-e",
            secret,
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


def test_vendor_schema_triggers(vendor_database):
    vendor, container, _port = vendor_database
    migration = import_module("rebac.migrations.0005_schema_generation")
    mysql = vendor == "mysql"
    statements = []
    quote = "`" if mysql else '"'
    editor = SimpleNamespace(
        connection=SimpleNamespace(vendor=vendor),
        quote_name=lambda name: f"{quote}{name}{quote}",
        execute=statements.append,
    )
    # Model state with custom db_table values proves the migration doesn't
    # assume the app's default table names.
    state = SimpleNamespace(
        get_model=lambda app, name: SimpleNamespace(
            _meta=SimpleNamespace(db_table=f"probe_{name.lower()}")
        )
    )
    witness = state.get_model("rebac", "SchemaGeneration")._meta.db_table
    tables = migration.tables(state)
    if mysql:
        statements.extend(["CREATE DATABASE schema_probe", "USE schema_probe"])
    statements.append(f"CREATE TABLE {witness} (id smallint PRIMARY KEY, revision varchar(32))")
    statements.extend(
        f"CREATE TABLE {table} (id bigint PRIMARY KEY, body varchar(100))" for table in tables
    )
    migration.install(state, editor)

    def remember(name):
        if mysql:
            return f"SET @{name} = (SELECT revision FROM {witness} WHERE id=1)"
        return f"SELECT revision AS {name} FROM {witness} WHERE id=1 \\gset"

    before = "@before" if mysql else ":'before'"
    temporary = "@temporary" if mysql else ":'temporary'"

    def verify(predicate, label):
        statements.append(
            f"SELECT CASE WHEN {predicate} THEN '{label}:ok' ELSE 'FAIL' END "
            f"FROM {witness} WHERE id=1"
        )

    for table in tables:
        statements.extend([remember("before"), f"INSERT INTO {table} VALUES (1, 'original')"])
        verify(f"revision<>{before}", "insert")
        statements.extend(
            [
                remember("before"),
                "START TRANSACTION",
                f"UPDATE {table} SET body='temporary' WHERE id=1",
                remember("temporary"),
                "ROLLBACK",
            ]
        )
        verify(f"revision={before}", "rollback")
        statements.append(f"UPDATE {table} SET body='committed' WHERE id=1")
        verify(f"revision<>{temporary} AND revision<>{before}", "new-revision")
        statements.extend([remember("before"), f"DELETE FROM {table} WHERE id=1"])
        verify(f"revision<>{before}", "delete")
    migration.uninstall(state, editor)
    migration.uninstall(state, editor)  # Idempotent reverse, including partial DDL.

    if mysql:
        sql = "DELIMITER //\n" + "\n".join(s + "//" for s in statements)
        client = ["mysql", "-uroot", "-pschema-test", "--batch", "--skip-column-names"]
    else:
        sql = "\n".join(s if s.endswith("\\gset") else s + ";" for s in statements)
        client = ["psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-tA"]
    result = _run(["docker", "exec", "-i", container, *client], input=sql)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    assert result.stdout.count(":ok") == 20


def test_vendor_django_schema_paths(vendor_database, django_db_blocker, monkeypatch):
    vendor, _container, port = vendor_database
    if vendor == "postgresql":
        try:
            import_module("psycopg")
        except ImportError:
            pytest.importorskip(
                "psycopg2", reason="Django PostgreSQL paths need psycopg or psycopg2"
            )
    else:
        pytest.importorskip("MySQLdb", reason="Django MySQL paths need the mysqlclient driver")

    from django.apps import apps
    from django.db import connections, transaction
    from django.db.utils import ConnectionHandler
    from django.test.utils import CaptureQueriesContext

    from rebac import LocalBackend
    from rebac.models.generation import SchemaGeneration
    from rebac.schema.generation import (
        missing_schema_triggers,
        refresh_schema_revision,
        schema_triggers_verified,
    )

    alias = f"schema_driver_{vendor}"
    handler = ConnectionHandler(
        {
            "default": {
                "ENGINE": "django.db.backends." + vendor,
                "NAME": "mysql" if vendor == "mysql" else "postgres",
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
    local = LocalBackend()
    migration = import_module("rebac.migrations.0005_schema_generation")
    monkeypatch.setattr("rebac.schema.generation._trigger_checks", {})
    try:
        with django_db_blocker.unblock():
            # Autocommit exercises the actual driver's missing-table exception
            # (sqlstate for psycopg, pgcode for psycopg2, errno for mysqlclient).
            assert local._read_schema_revision(db) is None
            with transaction.atomic(using=alias), CaptureQueriesContext(db) as queries:
                assert local._read_schema_revision(db) is None
                with db.cursor() as cursor:
                    cursor.execute("SELECT 1")  # Missing metadata did not abort the transaction.
                    assert cursor.fetchone()[0] == 1
            if vendor == "postgresql":
                assert any("to_regclass" in q["sql"] for q in queries)
            assert len(missing_schema_triggers(db)) == 15
            with db.schema_editor() as editor:
                editor.create_model(SchemaGeneration)
                for table in migration.tables(apps):
                    editor.execute(
                        f"CREATE TABLE {editor.quote_name(table)} "
                        "(id bigint PRIMARY KEY, body varchar(100))"
                    )
                migration.install(apps, editor)
            assert missing_schema_triggers(db) == []
            assert schema_triggers_verified(db)
            before = local._read_schema_revision(db)
            assert before is not None
            table = db.ops.quote_name(migration.tables(apps)[0])
            with db.cursor() as cursor:
                cursor.execute(f"TRUNCATE TABLE {table}")
            assert local._read_schema_revision(db) == before
            refresh_schema_revision(db)
            assert local._read_schema_revision(db) != before
            with db.schema_editor() as editor:
                migration.uninstall(apps, editor)
                migration.uninstall(apps, editor)
            assert len(missing_schema_triggers(db)) == 15
    finally:
        with django_db_blocker.unblock():
            db.close()
        del connections[alias]
