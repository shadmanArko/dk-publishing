"""Snapshot hashing. The hash is how the publish step proves it sends exactly what was approved."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical_json(content: Mapping[str, Any]) -> str:
    """Stable JSON: sorted keys, no whitespace, UTF-8 characters kept as written."""
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_hash(content: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON. Equal content always gives an equal hash."""
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()
