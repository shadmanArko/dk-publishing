"""Unattended upkeep: renewing the token that expires, and sweeping links nobody revoked."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from dk_publishing import composition


def meta_file(tmp_path: Path, **threads: str) -> str:
    path = tmp_path / "dk.json"
    path.write_text(json.dumps({"meta": {"threads": {"user_id": "TH1", **threads}}}))
    return f"{path}#meta"


def test_nothing_to_renew_is_not_an_error(tmp_path: Path) -> None:
    assert composition.renew_tokens({}) is None  # no Meta credentials at all
    assert composition.renew_tokens({"META_CREDENTIALS_FILE": str(tmp_path / "none.json")}) is None
    assert composition.renew_tokens({"META_CREDENTIALS_FILE": meta_file(tmp_path)}) is None
    (tmp_path / "other.json").write_text(json.dumps({"youtube": {}}))
    assert (
        composition.renew_tokens({"META_CREDENTIALS_FILE": f"{tmp_path}/other.json#meta"}) is None
    )


def test_a_token_refreshed_recently_is_left_alone(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    ref = meta_file(tmp_path, access_token="T", refreshed_at=datetime.now(UTC).isoformat())
    result = composition.renew_tokens({"META_CREDENTIALS_FILE": ref})
    assert result is not None and result.ok and not result.changed


def test_public_links_nobody_revoked_are_swept_but_fresh_ones_stay(tmp_path: Path) -> None:
    public = tmp_path / "public"
    old, fresh = public / "old-token", public / "fresh-token"
    for folder in (old, fresh):
        folder.mkdir(parents=True)
        (folder / "clip.mp4").write_bytes(b"x")
    aged = time.time() - 8 * 3600
    os.utime(old, (aged, aged))
    env = {"PUBLIC_MEDIA_DIR": str(public), "PUBLIC_MEDIA_BASE_URL": "https://m.example"}
    assert composition.purge_public_media(env) == 1
    assert not old.exists() and fresh.exists()


@pytest.mark.parametrize(
    "env", [{}, {"PUBLIC_MEDIA_DIR": "/x"}, {"PUBLIC_MEDIA_BASE_URL": "https://m"}]
)
def test_without_public_links_there_is_nothing_to_sweep(env: dict[str, str]) -> None:
    assert composition.purge_public_media(env) == 0
