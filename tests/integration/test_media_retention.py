"""Keeping the media disk from filling up: what is deleted, what never is, and the upload limits."""

from __future__ import annotations

import dataclasses
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dk_publishing.adapters.media.janitor import MediaDiskJanitor
from dk_publishing.application.ports import DiskUsage
from dk_publishing.application.use_cases.media_retention import disk_warning, purge_media
from dk_publishing.entrypoints.cli import main
from dk_publishing.entrypoints.publish_api.app import create_app
from tests.integration.test_api import Rig

DAY = timedelta(days=1)


@pytest.fixture
def rig(conninfo: str, tmp_path: Path) -> Rig:
    return Rig(conninfo, tmp_path)


def uploads(tmp_path: Path) -> list[Path]:
    folder = tmp_path / "media" / "uploads"
    return sorted(folder.iterdir()) if folder.exists() else []


def age_files(tmp_path: Path, days: int) -> None:
    """Pretend every file on the disk was last touched `days` before the test clock."""
    for path in uploads(tmp_path):
        moment = (datetime(2026, 11, 14, 12, tzinfo=UTC) - days * DAY).timestamp()
        os.utime(path, (moment, moment))


def purge(rig: Rig, tmp_path: Path, **kw: object) -> int:
    disk = MediaDiskJanitor(tmp_path / "media")
    result = purge_media(rig.api.core, disk, **kw)  # type: ignore[arg-type]
    return len(result.removed)


def with_limits(rig: Rig, **limits: object) -> None:
    rig.api = dataclasses.replace(rig.api, **limits)  # type: ignore[arg-type]
    rig.client = TestClient(create_app(rig.api))


class FullDisk:
    def usage(self) -> DiskUsage:
        return DiskUsage(total=100, used=90)

    def delete_unused(self, *a: object, **k: object) -> list[object]:
        return []


# --- what is deleted ---------------------------------------------------------------------------


def test_an_upload_no_post_used_is_removed_after_the_grace_period_and_says_so(
    rig: Rig, tmp_path: Path
) -> None:
    media = rig.upload()
    age_files(tmp_path, 30)
    rig.clock.advance(timedelta(days=30))
    assert purge(rig, tmp_path) == 1
    assert uploads(tmp_path) == []

    body = rig.reel("late")  # uploads a fresh copy...
    body["variants"][0]["media"] = [media]  # ...but names the removed one
    problems = rig.post(body).json()["problems"]
    expired = next(p for p in problems if p["code"] == "media_expired")
    assert "Upload it again" in expired["message"]


def test_a_waiting_draft_keeps_its_file_however_old(rig: Rig, tmp_path: Path) -> None:
    assert rig.post(rig.reel()).status_code == 201
    age_files(tmp_path, 60)
    assert purge(rig, tmp_path) == 0
    assert len(uploads(tmp_path)) == 1


def test_a_scheduled_post_keeps_its_file(rig: Rig, tmp_path: Path) -> None:
    assert rig.post(rig.reel(), "approve").status_code == 201
    age_files(tmp_path, 60)
    assert purge(rig, tmp_path) == 0


def test_a_finished_post_releases_its_file_only_after_the_grace_period(
    rig: Rig, tmp_path: Path
) -> None:
    assert rig.post(rig.reel()).status_code == 201
    assert rig.call("DELETE", "/v1/posts/campaign-1").status_code == 200  # cancelled
    age_files(tmp_path, 60)

    rig.clock.advance(timedelta(days=6))
    assert purge(rig, tmp_path) == 0  # cancelled 6 days ago: a retry is still plausible
    rig.clock.advance(timedelta(days=2))
    assert purge(rig, tmp_path) == 1


def test_a_file_shared_with_a_live_post_survives_when_another_post_ends(
    rig: Rig, tmp_path: Path
) -> None:
    media = rig.upload()
    for key in ("first", "second"):
        body = rig.reel(key)
        body["variants"][0]["media"] = [media]
        assert rig.post(body).status_code == 201
    assert rig.call("DELETE", "/v1/posts/first").status_code == 200
    age_files(tmp_path, 60)
    rig.clock.advance(timedelta(days=30))
    assert purge(rig, tmp_path) == 0  # "second" is still waiting


def test_a_dry_run_deletes_nothing(rig: Rig, tmp_path: Path) -> None:
    rig.upload()
    age_files(tmp_path, 30)
    rig.clock.advance(timedelta(days=30))
    assert purge(rig, tmp_path, dry_run=True) == 1
    assert len(uploads(tmp_path)) == 1


def test_a_recent_upload_waiting_for_its_post_is_kept(rig: Rig, tmp_path: Path) -> None:
    rig.clock.set(datetime.now(UTC))  # the upload's record is stamped by the database's clock
    rig.upload()
    age_files(tmp_path, 30)
    assert purge(rig, tmp_path) == 0


# --- the disk itself (no database) -------------------------------------------------------------


def test_drive_downloads_and_abandoned_parts_are_cleaned_too(tmp_path: Path) -> None:
    originals = tmp_path / "originals"
    originals.mkdir()
    (originals / "abc123-dish.mp4").write_bytes(b"in use")
    (originals / "def456-old.mp4").write_bytes(b"unused")
    fresh = originals / "zzz-new.mp4.part"
    fresh.write_bytes(b"half")  # being written right now
    stale = originals / "zzz-old.mp4.part"
    stale.write_bytes(b"half")
    long_ago = (datetime.now(UTC) - 3 * DAY).timestamp()
    for path in (originals / "abc123-dish.mp4", originals / "def456-old.mp4", stale):
        os.utime(path, (long_ago, long_ago))

    keep = [{"name": "dish.mp4", "drive_file_id": "F1", "md5": "abc123"}]
    disk = MediaDiskJanitor(tmp_path)
    names = [f.name for f in disk.delete_unused(keep, datetime.now(UTC) - DAY, dry_run=True)]
    assert names == ["originals/def456-old.mp4", "originals/zzz-old.mp4.part"]
    disk.delete_unused(keep, datetime.now(UTC) - DAY)
    assert sorted(p.name for p in originals.iterdir()) == [
        "abc123-dish.mp4",
        "zzz-new.mp4.part",
    ]


def test_the_warning_wording_is_stable_within_a_five_percent_step() -> None:
    assert disk_warning(DiskUsage(100, 69)) is None
    assert disk_warning(DiskUsage(100, 71)) == disk_warning(DiskUsage(100, 74))
    assert disk_warning(DiskUsage(100, 71)) != disk_warning(DiskUsage(100, 75))
    refused = disk_warning(DiskUsage(100, 88))
    assert refused is not None and "refused" in refused


# --- upload limits -----------------------------------------------------------------------------


def test_uploads_are_refused_while_the_disk_is_nearly_full(rig: Rig) -> None:
    with_limits(rig, disk=FullDisk(), disk_stop_percent=85)
    response = rig.call("POST", "/v1/media?filename=a.mp4", content=b"x")
    assert response.status_code == 507
    assert response.json()["problems"][0]["code"] == "storage_full"
    assert rig.call("GET", "/v1/platforms", "read").status_code == 200  # everything else works


def test_a_key_cannot_keep_more_than_its_quota(rig: Rig, tmp_path: Path) -> None:
    with_limits(rig, key_quota_bytes=20)
    assert rig.call("POST", "/v1/media?filename=a.mp4", content=b"x" * 12).status_code == 201
    over = rig.call("POST", "/v1/media?filename=b.mp4", content=b"y" * 12)  # only 8 bytes left
    assert over.status_code == 422 and over.json()["problems"][0]["code"] == "too_large"
    assert rig.call("POST", "/v1/media?filename=c.mp4", content=b"z" * 8).status_code == 201
    full = rig.call("POST", "/v1/media?filename=d.mp4", content=b"w")
    assert full.status_code == 413 and full.json()["problems"][0]["code"] == "quota_exceeded"
    other = rig.call("POST", "/v1/media?filename=e.mp4", "approve", content=b"v")
    assert other.status_code == 201  # the quota is per key


def test_the_purge_command_lists_what_it_would_delete(
    conninfo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig = Rig(conninfo, tmp_path)
    rig.upload()
    age_files(tmp_path, 400)
    config = tmp_path / "dk.json"
    config.write_text("{}")
    monkeypatch.setenv("DATABASE_URL", conninfo)
    monkeypatch.setenv("DK_CONFIG_FILE", str(config))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_RETENTION_DAYS", "0")
    assert main(["media", "purge", "--dry-run"]) == 0
    assert "would delete uploads/" in capsys.readouterr().out
    assert len(uploads(tmp_path)) == 1
    assert main(["media", "purge"]) == 0
    assert uploads(tmp_path) == []
