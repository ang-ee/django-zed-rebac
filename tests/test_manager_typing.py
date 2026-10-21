"""Native django-stubs must see both queryset-derived manager inheritance chains."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.mark.slow
def test_queryset_manager_extensions_keep_native_type_checks(tmp_path: Path) -> None:
    """Custom methods stay typed without erasing ordinary Django field errors."""

    source = tmp_path.resolve() / "manager_contract.py"
    source.write_text(
        dedent(
            """\
            from typing import assert_type

            from django.db import models

            from rebac import RebacManager, RebacQuerySet, TrackedManager, TrackedQuerySet
            from tests.testapp.models import Post

            class PostQuerySet[T: models.Model](RebacQuerySet[T]):
                def answer(self) -> int:
                    return 42

            PostManagerBase = RebacManager.from_queryset(PostQuerySet)

            class PostManager(PostManagerBase[Post]):
                pass

            class TrackedPostQuerySet[T: models.Model](TrackedQuerySet[T]):
                def label(self) -> str:
                    return "tracked"

            TrackedPostManagerBase = TrackedManager.from_queryset(TrackedPostQuerySet)

            class TrackedPostManager(TrackedPostManagerBase[Post]):
                pass

            assert_type(PostManager().answer(), int)
            assert_type(TrackedPostManager().label(), str)
            assert_type(PostManager().get(), Post)
            assert_type(TrackedPostManager().get(), Post)
            assert_type(Post.objects.get(), Post)
            assert_type(Post.objects.get().title, str)
            assert_type(Post.objects.with_actor(None).get(), Post)
            post = Post()
            post.title = object()  # invalid-field
            Post.objects.get().title = object()  # invalid-manager-field
            PostManager().get().absent  # missing-row-field
            TrackedPostManager().get().absent  # missing-tracked-row-field
            PostManager().absent()  # missing-scoped
            TrackedPostManager().absent()  # missing-tracked
            """
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--follow-imports=silent",
            "--show-error-codes",
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
            str(source),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    errors = [line for line in result.stdout.splitlines() if ": error:" in line]
    expected = {
        "invalid-field": "assignment",
        "invalid-manager-field": "assignment",
        "missing-row-field": "attr-defined",
        "missing-tracked-row-field": "attr-defined",
        "missing-scoped": "attr-defined",
        "missing-tracked": "attr-defined",
    }
    assert len(errors) == len(expected), result.stdout + result.stderr
    for marker, code in expected.items():
        number = next(
            number
            for number, line in enumerate(source.read_text().splitlines(), 1)
            if f"# {marker}" in line
        )
        assert any(
            line.startswith(f"{source}:{number}: error:") and line.endswith(f"[{code}]")
            for line in errors
        ), result.stdout


def test_managers_keep_native_serialization() -> None:
    """Queryset-derived public managers retain their importable migration identity."""

    from django.db import models

    from rebac import RebacManager, TrackedManager

    for manager in (RebacManager[models.Model](), TrackedManager[models.Model]()):
        as_manager, path, queryset_path, args, kwargs = manager.deconstruct()
        assert as_manager is False
        assert path == f"rebac.managers.{type(manager).__name__}"
        assert queryset_path is None
        assert args == ()
        assert kwargs == {}
