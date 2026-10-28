"""Upstream django-stubs must see both queryset-derived manager inheritance chains."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.mark.slow
def test_queryset_manager_extensions_keep_native_type_checks(tmp_path: Path) -> None:
    """Custom methods stay typed without erasing ordinary Django field errors."""

    app = tmp_path / "typing_app"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (app / "models.py").write_text(
        dedent(
            """\
            from django.db import models
            from rebac import RebacManager, RebacMixin

            class OwnManager[M: models.Model](RebacManager[M]):
                def label(self) -> str:
                    return "own"

            class OwnRecord(RebacMixin, models.Model):
                objects = OwnManager()
                title = models.CharField(max_length=50)
            """
        )
    )
    (tmp_path / "typing_settings.py").write_text(
        'from tests.settings import *\nINSTALLED_APPS = [*INSTALLED_APPS, "typing_app"]\n'
    )
    config = tmp_path / "mypy.ini"
    config.write_text(
        "[mypy]\nplugins = mypy_django_plugin.main\n"
        "[mypy.plugins.django-stubs]\ndjango_settings_module = typing_settings\n"
    )
    source = tmp_path.resolve() / "manager_contract.py"
    source.write_text(
        dedent(
            """\
            from typing import assert_type

            from django.db import models

            from rebac import RebacManager, RebacQuerySet, TrackedManager, TrackedQuerySet
            from tests.testapp.models import Post
            from typing_app.models import OwnRecord

            class PostQuerySet[T: models.Model](RebacQuerySet[T]):
                def answer(self) -> int:
                    return 42

            class OtherPostQuerySet[T: models.Model](RebacQuerySet[T]):
                def other(self) -> str:
                    return "other"

            PostManagerBase = RebacManager.from_queryset(PostQuerySet)

            class PostManager(PostManagerBase[Post]):
                pass

            class TrackedPostQuerySet[T: models.Model](TrackedQuerySet[T]):
                def label(self) -> str:
                    return "tracked"

            TrackedPostManagerBase = TrackedManager.from_queryset(TrackedPostQuerySet)

            class TrackedPostManager(TrackedPostManagerBase[Post]):
                pass

            queryset = PostQuerySet[Post](model=Post)
            assert_type(queryset.with_actor(None), PostQuerySet[Post])
            assert_type(queryset.as_user(None), PostQuerySet[Post])
            assert_type(queryset.as_agent(None), PostQuerySet[Post])
            assert_type(queryset.with_action("read"), PostQuerySet[Post])
            assert_type(queryset.on_field_deny("allow"), PostQuerySet[Post])
            assert_type(queryset.for_write(), PostQuerySet[Post])
            assert_type(queryset.scoped(), PostQuerySet[Post])
            assert_type(queryset.scoped_for_aggregate(), PostQuerySet[Post])
            assert_type(queryset.sudo(reason="typing"), PostQuerySet[Post])
            assert_type(queryset.system_context(reason="typing"), PostQuerySet[Post])
            assert_type(queryset.rebac_select_related(), PostQuerySet[Post])
            assert_type(queryset.rebac_prefetch_related(), PostQuerySet[Post])
            assert_type(queryset.with_actor(None).sudo(reason="typing").answer(), int)
            assert_type(queryset & queryset, PostQuerySet[Post])
            assert_type(queryset | queryset, PostQuerySet[Post])
            assert_type(queryset ^ queryset, PostQuerySet[Post])
            assert_type(queryset & queryset.none(), PostQuerySet[Post])
            assert_type(queryset.none() | queryset, PostQuerySet[Post])
            assert_type(queryset.none() ^ queryset, PostQuerySet[Post])
            other_queryset = OtherPostQuerySet[Post](model=Post)
            assert_type(queryset & other_queryset, RebacQuerySet[Post])
            assert_type(queryset | other_queryset, RebacQuerySet[Post])
            assert_type(queryset ^ other_queryset, RebacQuerySet[Post])
            assert_type((queryset | queryset).answer(), int)
            assert_type(queryset.with_actor(None).get(), Post)
            assert_type(queryset.sudo(reason="typing").get().title, str)
            assert_type(PostManager().answer(), int)
            assert_type(TrackedPostManager().label(), str)
            # Upstream django-stubs types get_queryset() on a from_queryset
            # manager as Django's QuerySet; the verbs on the manager itself
            # keep the custom type (below).
            assert_type(PostManager().get_queryset().get(), Post)
            assert_type(TrackedPostManager().get_queryset().get(), Post)
            assert_type(PostManager().with_actor(None), PostQuerySet[Post])
            assert_type(PostManager().with_actor(None).answer(), int)
            assert_type(PostManager().get(), Post)
            assert_type(TrackedPostManager().get(), Post)
            assert_type(Post.objects.get(), Post)
            assert_type(Post.objects.get().title, str)
            assert_type(Post.objects.with_actor(None), RebacQuerySet[Post])
            assert_type(Post.objects.with_actor(None).get(), Post)
            assert_type(OwnRecord.objects.label(), str)
            assert_type(OwnRecord.objects.get(), OwnRecord)
            assert_type(OwnRecord.objects.get().title, str)
            assert_type(OwnRecord.objects.with_actor(None), RebacQuerySet[OwnRecord])
            assert_type(OwnRecord.objects.with_actor(None).get(), OwnRecord)
            OwnRecord.objects.get().title = object()  # invalid-own-field
            post = Post()
            post.title = object()  # invalid-field
            Post.objects.get().title = object()  # invalid-manager-field
            PostManager().get().absent  # missing-row-field
            TrackedPostManager().get().absent  # missing-tracked-row-field
            PostManager().absent()  # missing-scoped
            TrackedPostManager().absent()  # missing-tracked
            PostManager().get_queryset().absent()  # missing-scoped-getter
            TrackedPostManager().get_queryset().absent()  # missing-tracked-getter
            (queryset | queryset).absent()  # missing-boolean-method
            (queryset | other_queryset).answer()  # missing-mixed-boolean-method
            """
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--config-file",
            str(config),
            "--follow-imports=silent",
            "--show-error-codes",
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
            str(source),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                filter(None, [str(tmp_path), os.environ.get("PYTHONPATH", "")])
            ),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    errors = [line for line in result.stdout.splitlines() if ": error:" in line]
    expected = {
        "invalid-own-field": "assignment",
        "invalid-field": "assignment",
        "invalid-manager-field": "assignment",
        "missing-row-field": "attr-defined",
        "missing-tracked-row-field": "attr-defined",
        "missing-scoped": "attr-defined",
        "missing-tracked": "attr-defined",
        "missing-scoped-getter": "attr-defined",
        "missing-tracked-getter": "attr-defined",
        "missing-boolean-method": "attr-defined",
        "missing-mixed-boolean-method": "attr-defined",
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


def test_native_factory_getters_keep_queryset_class_and_model() -> None:
    """Scoped and tracked getters use Django's configured queryset factory."""
    from rebac import RebacManager, RebacQuerySet, TrackedManager, TrackedQuerySet
    from tests.testapp.models import Post

    class PostQuerySet(RebacQuerySet[Post]):
        def answer(self) -> int:
            return 42

    class TrackedPostQuerySet(TrackedQuerySet[Post]):
        def label(self) -> str:
            return "tracked"

    scoped = RebacManager.from_queryset(PostQuerySet)()
    scoped.model = Post
    tracked = TrackedManager.from_queryset(TrackedPostQuerySet)()
    tracked.model = Post

    scoped_rows = scoped.get_queryset()
    tracked_rows = tracked.get_queryset()
    assert type(scoped_rows) is PostQuerySet
    assert type(tracked_rows) is TrackedPostQuerySet
    assert scoped_rows.model is tracked_rows.model is Post
    assert scoped_rows.answer() == 42
    assert tracked_rows.label() == "tracked"


def test_queryset_scope_clones_keep_concrete_class() -> None:
    """Django's clone constructor retains consumer queryset methods and its model."""
    from rebac import RebacQuerySet, SubjectRef
    from tests.testapp.models import Post

    class PostQuerySet(RebacQuerySet[Post]):
        def answer(self) -> int:
            return 42

    original = PostQuerySet(model=Post)
    actor = SubjectRef("auth/user", "typing")
    for clone in (
        original.with_actor(actor),
        original.as_user(actor),
        original.with_action("read"),
        original.on_field_deny("allow"),
        original.for_write(),
        original.sudo(reason="typing"),
        original.system_context(reason="typing"),
        original.rebac_select_related(None),
        original.rebac_prefetch_related(None),
    ):
        assert type(clone) is PostQuerySet
        assert clone is not original
        assert clone.model is Post
        assert clone.answer() == 42
    assert original.actor() is None
    assert not original.is_sudo()
