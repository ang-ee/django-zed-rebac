"""Canonical JSON for schema payloads, preserving datetime precision."""

import hashlib
import json
from datetime import datetime
from typing import Any


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    raise TypeError(f"Unexpected schema payload {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
