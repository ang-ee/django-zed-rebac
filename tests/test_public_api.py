"""The public import surface: ARCHITECTURE.md § Public API surface is the one list, and `__all__` matches it."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied

import rebac

ROOT = Path(__file__).resolve().parents[1]


def _architecture_block() -> str:
    """The one public API list: the code block under ARCHITECTURE.md § Public API surface."""
    text = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    match = re.search(r"## Public API surface\n\n```python\n(.*?)```", text, re.S)
    assert match, "ARCHITECTURE.md § Public API surface has no code block"
    return match.group(1)


def _documented_surface() -> dict[str, set[str]]:
    block = _architecture_block()
    surface: dict[str, set[str]] = {}
    top = re.search(r"from rebac import \((.*?)\n\)", block, re.S)
    assert top
    names = re.sub(r"#[^\n]*", "", top.group(1))
    surface["rebac"] = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", names))
    for module, imported in re.findall(
        r"^from (rebac\.[a-z_.]+)\s+import\s+([^#\n]+)", block, re.M
    ):
        surface.setdefault(module, set()).update(
            name.strip() for name in imported.split(",") if name.strip()
        )
    return surface


ARCHITECTURE_SURFACE = {module: sorted(names) for module, names in _documented_surface().items()}

PUBLIC_ERRORS = [
    "PermissionDenied",
    "MissingActorError",
    "CaveatUnsupportedError",
    "PermissionDepthExceeded",
    "NoActorResolvedError",
    "RelationshipReadError",
    "SchemaError",
]


def _pairs(surface: dict[str, list[str]]) -> list[tuple[str, str]]:
    return [(module, name) for module, names in surface.items() for name in names]


@pytest.mark.parametrize("name", rebac.__all__)
def test_every_exported_name_resolves(name: str) -> None:
    assert getattr(rebac, name) is not None


def test_exports_are_unique() -> None:
    assert len(rebac.__all__) == len(set(rebac.__all__))


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no attribute 'not_public'"):
        _ = rebac.not_public  # type: ignore[attr-defined]


@pytest.mark.parametrize(("module", "name"), _pairs(ARCHITECTURE_SURFACE))
def test_architecture_public_surface_is_importable(module: str, name: str) -> None:
    assert getattr(importlib.import_module(module), name) is not None
    if module == "rebac":
        assert name in rebac.__all__


def test_every_export_is_in_the_architecture_block() -> None:
    """AGENTS.md § Public API surface: a name exported and absent from the block is a spec bug."""
    documented = set(ARCHITECTURE_SURFACE["rebac"])
    assert sorted(set(rebac.__all__) - documented) == []
    assert sorted(documented - set(rebac.__all__)) == []


def test_agents_md_points_at_the_one_list() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "The list lives in one place" in text
    assert "from rebac import (" not in text


def test_celery_adapter_is_not_shipped() -> None:
    """ARCHITECTURE.md § Celery: automatic propagation is planned, not shipped."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("rebac.celery")


@pytest.mark.parametrize("name", PUBLIC_ERRORS)
def test_public_errors_share_the_rebac_base(name: str) -> None:
    error = getattr(rebac, name)
    assert issubclass(error, rebac.RebacError)
    assert issubclass(error, Exception)


def test_permission_denied_is_djangos_permission_denied() -> None:
    assert issubclass(rebac.PermissionDenied, DjangoPermissionDenied)
    with pytest.raises(DjangoPermissionDenied):
        raise rebac.PermissionDenied("denied")


def test_sudo_errors_share_the_rebac_base() -> None:
    from rebac.errors import SudoNotAllowedError, SudoReasonRequiredError

    assert issubclass(SudoNotAllowedError, rebac.RebacError)
    assert issubclass(SudoReasonRequiredError, rebac.RebacError)
