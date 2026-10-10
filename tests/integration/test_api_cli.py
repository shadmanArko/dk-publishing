"""`dk api ...`: keys and approvals from the terminal, and the media store that routes uploads."""

from __future__ import annotations

from pathlib import Path

import pytest

from dk_publishing import composition
from dk_publishing.adapters.media.uploads import CompositeMediaStore, UploadStore
from dk_publishing.application.use_cases import api_keys
from dk_publishing.domain.errors import Rejected
from dk_publishing.domain.publishing import Rendition
from dk_publishing.entrypoints.cli import main
from tests.integration.test_api import Rig


def empty_config(tmp_path: Path) -> str:
    path = tmp_path / "dk.json"
    path.write_text("{}")
    return str(path)


def test_keys_are_made_listed_and_revoked_from_the_terminal(
    conninfo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", conninfo)
    monkeypatch.setenv("DK_CONFIG_FILE", empty_config(tmp_path))
    assert main(["api", "key-create", "agent", "--right", "draft"]) == 0
    printed = capsys.readouterr().out
    key = next(line for line in printed.splitlines() if line.startswith("dkp_"))
    assert main(["api", "key-create", "agent"]) == 1  # the name is taken while it is live
    assert "revoke it first" in capsys.readouterr().err

    assert main(["api", "keys"]) == 0
    assert "agent" in capsys.readouterr().out
    assert main(["api", "key-revoke", "agent"]) == 0
    assert main(["api", "key-revoke", "agent"]) == 1
    assert key.startswith(api_keys.PREFIX)
    assert main(["api", "key-create", "agent"]) == 0  # the name is free again


def test_an_approval_from_the_terminal(
    conninfo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig = Rig(conninfo, tmp_path)
    assert rig.post(rig.reel("by-agent")).status_code == 201
    monkeypatch.setenv("DATABASE_URL", conninfo)
    monkeypatch.setenv("DK_CONFIG_FILE", empty_config(tmp_path))
    monkeypatch.setattr(composition, "build_api_services", lambda *a, **k: rig.api)
    assert main(["api", "approve", "nothing-here"]) == 1
    assert main(["api", "approve", "by-agent"]) == 0
    assert "by-agent: scheduled" in capsys.readouterr().out
    assert main(["api", "approve", "by-agent"]) == 1  # nothing left to approve
    assert "nothing waiting" in capsys.readouterr().err


def test_the_media_store_routes_uploads_and_drive_files(tmp_path: Path) -> None:
    uploads = UploadStore(tmp_path)
    writer = uploads.begin("a.mp4")
    writer.write(b"hello")
    stored = writer.finish()

    class Drive:
        def ensure_local(self, media):  # type: ignore[no-untyped-def]
            return [Rendition("original", f"/drive/{m['name']}", "x") for m in media]

    store = CompositeMediaStore(Drive(), uploads)
    items = [
        {"name": "from-drive.mp4", "drive_file_id": "F"},
        {"name": stored.filename, "media_id": "m1", "sha256": stored.sha256},
    ]
    paths = [r.path for r in store.ensure_local(items)]
    assert paths[0] == "/drive/from-drive.mp4"
    assert paths[1].endswith(f"{stored.sha256}-a.mp4") and Path(paths[1]).read_bytes() == b"hello"

    with pytest.raises(Rejected, match="Google Drive"):
        CompositeMediaStore(None, uploads).ensure_local(items[:1])
    gone = [{"name": "a.mp4", "media_id": "m", "sha256": "0" * 64}]
    with pytest.raises(Rejected, match="no longer on the server"):
        store.ensure_local(gone)
