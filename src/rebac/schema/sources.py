"""Resolve app-owned Zed schema sources without loading runtime policy."""

from pathlib import Path
from typing import Any


def resolve_schema_path(app_config: Any) -> Path | None:
    """Resolve an app's relative/absolute source; absent/None uses permissions.zed.

    Missing files return None. A path to a directory is a configuration error,
    not an absent source. Read/permission errors remain visible to the caller.
    """
    root = getattr(app_config, "path", None)
    if root is None:
        return None
    declared = getattr(app_config, "rebac_schema", None)
    path = Path(root) / ("permissions.zed" if declared is None else declared)
    if not path.exists():
        return None
    if not path.is_file():
        raise IsADirectoryError(f"REBAC schema source is not a file: {path}")
    return path
