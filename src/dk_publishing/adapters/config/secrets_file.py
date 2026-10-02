"""A JSON file that holds secrets, either on its own or as one section of the single `dk.json`.

A reference is `path` or `path#section`. The section form lets every credentials class keep reading
"its own" object while all of them live in one file. Writes (a renewed token, a saved refresh
token) change only that section, atomically, and keep the file owner-only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dk_publishing.adapters.config.platforms import ConfigError


def make_ref(path: Path | str, section: str) -> str:
    return f"{path}#{section}"


def split_ref(ref: Path | str) -> tuple[Path, str | None]:
    text = str(ref)
    base, sep, section = text.partition("#")
    return Path(base).expanduser(), (section or None) if sep else None


class SecretsFile:
    def __init__(self, ref: Path | str, *, what: str = "credentials") -> None:
        self.path, self.section = split_ref(ref)
        self._what = what

    def _document(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except OSError:
            raise ConfigError(f"cannot read the {self._what} file {self.path}") from None
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{self.path} is not valid JSON (line {exc.lineno})") from None
        if not isinstance(data, dict):
            raise ConfigError(f"{self.path} must hold a JSON object")
        return data

    def read(self) -> dict[str, Any]:
        """The whole file, or just this reference's section."""
        data = self._document()
        if self.section is None:
            return data
        part = data.get(self.section)
        if not isinstance(part, dict):
            raise ConfigError(f"{self.path} has no '{self.section}' section; run `dk init`")
        return part

    def update(self, changes: Mapping[str, Any]) -> None:
        """Merge `changes` into the reference's object and write the file back atomically."""
        data = self._document()
        target = data
        if self.section is not None:
            part = data.get(self.section)
            if not isinstance(part, dict):
                raise ConfigError(f"{self.path} has no '{self.section}' section; run `dk init`")
            target = part
        target.update(changes)
        write_private(self.path, data)


def write_private(path: Path, data: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)  # the old file is never left half-written
