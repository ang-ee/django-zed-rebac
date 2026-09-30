"""``manage.py rebac check`` and ``manage.py rebac explain``.

ARCHITECTURE.md § Management commands: ``check`` validates the installed
schema sources without writes; ``explain <type>.<perm>`` prints the compiled
expression.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError

from rebac import backend
from rebac.backends import reset_backend
from rebac.models import SchemaDefinition, SchemaOverride, SchemaPermission
from rebac.schema.parser import parse_permission_expression


def _replace_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> None:
    """Serve ``text`` as the testapp's schema source for the command."""

    def resolve(app_config: Any) -> Path | None:
        if app_config.label != "testapp":
            return None
        path = tmp_path / "permissions.zed"
        path.write_text(text, encoding="utf-8")
        return path

    monkeypatch.setattr("rebac.management.commands.rebac.resolve_schema_path", resolve)


def _run(*args: str) -> tuple[str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    call_command("rebac", *args, stdout=stdout, stderr=stderr)
    return stdout.getvalue(), stderr.getvalue()


# ---------- check ----------


def test_check_accepts_the_installed_schema() -> None:
    stdout, stderr = _run("check")
    assert stdout.strip() == "OK"
    assert stderr == ""


def test_check_writes_nothing(db) -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as queries:
        _run("check")
    writes = ("INSERT", "UPDATE", "DELETE", "REPLACE")
    assert not [q["sql"] for q in queries.captured_queries if q["sql"].lstrip().startswith(writes)]
    assert not SchemaDefinition.objects.filter(resource_type="blog/post").exists()


def test_check_fails_on_a_parse_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _replace_sources(
        monkeypatch, tmp_path, "definition blog/post {\n    relation owner auth/user\n}\n"
    )
    stderr = io.StringIO()
    with pytest.raises(CommandError, match="Schema check failed"):
        call_command("rebac", "check", stdout=io.StringIO(), stderr=stderr)
    assert "tests.testapp" in stderr.getvalue()
    assert "line 2" in stderr.getvalue()


def test_check_fails_on_a_validation_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _replace_sources(
        monkeypatch,
        tmp_path,
        "definition blog/post {\n    relation owner: auth/user\n    permission read = nobody\n}\n",
    )
    stderr = io.StringIO()
    with pytest.raises(CommandError, match="Schema check failed"):
        call_command("rebac", "check", stdout=io.StringIO(), stderr=stderr)
    assert "undefined reference 'nobody'" in stderr.getvalue()


# ---------- explain ----------


def _explained_expression(target: str) -> Any:
    stdout, _stderr = _run("explain", target)
    resource_type, permission = target.rsplit(".", 1)
    prefix = f"{resource_type}#{permission} = "
    assert stdout.startswith(prefix), stdout
    return parse_permission_expression(stdout.removeprefix(prefix).strip())


def _effective_expression(resource_type: str, permission: str) -> Any:
    reset_backend()
    perm = backend().schema().get_permission(resource_type, permission)
    assert perm is not None
    return perm.expression


@pytest.fixture
def synced(db) -> None:
    _run("sync")


def test_explain_prints_the_compiled_expression(synced) -> None:
    assert _explained_expression("blog/post.read") == _effective_expression("blog/post", "read")


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("blog/nothing.read", "No definition: blog/nothing"),
        ("blog/post.publish", "No permission 'publish' on blog/post"),
        ("blog/post", r"explain target must be <type>\.<perm>"),
    ],
)
def test_explain_misses_fail(synced, target: str, message: str) -> None:
    with pytest.raises(CommandError, match=message):
        _run("explain", target)


def test_explain_includes_an_active_override(synced) -> None:
    target = SchemaPermission.objects.get(definition__resource_type="blog/post", name="read")
    SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_TIGHTEN,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=target.pk,
        expression="owner",
        reason="owners only while under review",
    )
    effective = _effective_expression("blog/post", "read")
    assert effective != parse_permission_expression(target.expression)
    assert _explained_expression("blog/post.read") == effective
