"""Where an adapter gets its access token.

Interim: a token kept in a file outside the repository. It is read on every call, so rotating
the token needs no restart. The encrypted token vault replaces this when the connect flow exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from dk_publishing.domain.errors import AuthFailed


class TokenProvider(Protocol):
    def token(self) -> str: ...


class FileTokenProvider:
    """A file holding either the bare token or JSON with an `access_token` field."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def token(self) -> str:
        try:
            text = self._path.read_text().strip()
        except OSError:
            raise AuthFailed(f"cannot read the token file {self._path}") from None
        if text.startswith("{"):
            try:
                text = str(json.loads(text).get("access_token", "")).strip()
            except json.JSONDecodeError:
                raise AuthFailed(f"{self._path} is not valid JSON") from None
        if not text:
            raise AuthFailed(f"the token file {self._path} is empty")
        return text
