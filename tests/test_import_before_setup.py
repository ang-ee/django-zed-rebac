"""Which modules import before Django is set up.

An application imports ``rebac.managers`` (and what it re-exports) at the top of
its own modules, and test plugins import those before ``django.setup()``. A
model cannot be defined then, so a module on that path must not import
``rebac.models`` at import time. 0.36.0 broke it: ``rebac.managers`` imported
two new modules that imported the relationship model.

Every module of the package must import with no settings configured, except the
ones listed here, which define models or read settings as they load. A new
module that needs the registry is added to the list on purpose, with what
imports it checked.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import rebac

# Modules that cannot import before the app registry is populated, and why.
NEEDS_APPS = {
    "rebac.models": "defines the library's models",
    "rebac.models.audit": "defines a model",
    "rebac.models.generation": "defines a model",
    "rebac.models.overrides": "defines a model",
    "rebac.models.provenance": "defines a model",
    "rebac.models.relationship": "defines a model",
    "rebac.models.resource": "defines a model",
    "rebac.models.schema": "defines a model",
    "rebac.models.schema_write": "imports the schema models",
    "rebac.admin": "registers model admins",
    "rebac.mixins": "defines the abstract model mixin, which imports the models",
    "rebac.permissions_mixin": "builds on the model mixin",
    "rebac.compile.conditions": "imports the relationship model",
    "rebac.compile.evaluate": "imports the relationship model",
    "rebac.compile.read": "imports the relationship model",
    "rebac.drf": "rest_framework reads its settings as it loads",
    "rebac.management.commands.rebac": "imports the models",
}

_SCRIPT = """
import importlib, sys

skipped = []
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as error:
        # An optional dependency that is not installed here.
        if (error.name or "").split(".")[0] == "rebac":
            raise
        skipped.append(name)

from django.apps import apps
from django.conf import settings

assert not settings.configured, "a module configured settings as it loaded"
assert not apps.apps_ready
# The path an application takes: the queryset class, before any model exists.
from rebac.managers import RebacManager, RebacQuerySet

assert "rebac.models" not in sys.modules, "rebac.models was imported"
print(",".join(skipped))
"""


def _modules() -> list[str]:
    root = Path(rebac.__file__).parent
    names = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root.parent).with_suffix("").parts
        if "migrations" in parts:
            continue
        names.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def _run(names: list[str]) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    return subprocess.run(
        [sys.executable, "-c", _SCRIPT, *names],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
        env=environment,
    )


def test_every_other_module_imports_before_django_is_set_up() -> None:
    modules = _modules()
    assert set(NEEDS_APPS) <= set(modules), sorted(set(NEEDS_APPS) - set(modules))
    assert "rebac.managers" in modules and "rebac.managers" not in NEEDS_APPS
    result = _run([name for name in modules if name not in NEEDS_APPS])
    assert result.returncode == 0, result.stderr[-2000:]


def test_the_listed_modules_do_need_the_registry() -> None:
    """The list holds no module that has stopped needing it: such a line would hide a
    regression of that module."""
    for name in NEEDS_APPS:
        environment = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        result = subprocess.run(
            [sys.executable, "-c", f"import {name}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            env=environment,
        )
        assert result.returncode != 0, f"{name} imports before setup; remove it from NEEDS_APPS"
        assert "ImproperlyConfigured" in result.stderr or "ModuleNotFoundError" in result.stderr, (
            name,
            result.stderr[-500:],
        )
