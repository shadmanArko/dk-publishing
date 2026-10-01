from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

import pytest

from dk_publishing.adapters.media.drive_store import DriveMediaStore
from dk_publishing.domain.errors import Rejected, Retryable

VIDEO = b"\x00\x01 not really a video, but bytes " * 1000


class FakeDownloader:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.calls: list[str] = []

    def download(self, file_id: str, sink: BinaryIO) -> None:
        self.calls.append(file_id)
        data = self.files[file_id]
        for i in range(0, len(data), 4096):  # arrives in pieces, like the real download
            sink.write(data[i : i + 4096])


def item(
    name: str = "eid.mp4", file_id: str | None = "F1", data: bytes = VIDEO
) -> dict[str, object]:
    return {"name": name, "drive_file_id": file_id, "md5": hashlib.md5(data).hexdigest()}


def test_a_file_is_downloaded_verified_and_described(tmp_path: Path) -> None:
    store = DriveMediaStore(FakeDownloader({"F1": VIDEO}), tmp_path)
    [rendition] = store.ensure_local([item()])
    assert Path(rendition.path).read_bytes() == VIDEO
    assert rendition.profile == "original" and rendition.sha256 == hashlib.sha256(VIDEO).hexdigest()
    assert Path(rendition.path).parent == tmp_path / "originals"
    assert not list((tmp_path / "originals").glob("*.part"))  # no half-written leftovers


def test_a_file_already_downloaded_is_reused_not_fetched_again(tmp_path: Path) -> None:
    downloader = FakeDownloader({"F1": VIDEO})
    store = DriveMediaStore(downloader, tmp_path)
    first = store.ensure_local([item()])
    second = store.ensure_local([item()])
    assert downloader.calls == ["F1"] and first == second


def test_a_file_changed_in_drive_after_approval_is_refused_and_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    changed = DriveMediaStore(FakeDownloader({"F1": VIDEO + b"edited"}), tmp_path)
    with pytest.raises(Retryable, match="changed in Drive after it was approved"):
        changed.ensure_local([item()])
    assert list((tmp_path / "originals").iterdir()) == []


def test_a_replaced_file_gets_its_own_copy_because_it_is_named_by_checksum(tmp_path: Path) -> None:
    new = VIDEO + b"v2"
    store = DriveMediaStore(FakeDownloader({"F1": VIDEO, "F2": new}), tmp_path)
    old_path = store.ensure_local([item()])[0].path
    new_path = store.ensure_local([item(file_id="F2", data=new)])[0].path
    assert old_path != new_path and Path(old_path).read_bytes() == VIDEO


def test_a_name_cannot_escape_the_media_folder(tmp_path: Path) -> None:
    store = DriveMediaStore(FakeDownloader({"F1": VIDEO}), tmp_path)
    [rendition] = store.ensure_local([item(name="../../etc/evil name?.mp4")])
    assert Path(rendition.path).parent == tmp_path / "originals"
    assert "/" not in Path(rendition.path).name and Path(rendition.path).name.endswith(
        "evil_name_.mp4"
    )


def test_a_file_that_was_never_found_in_drive_is_rejected(tmp_path: Path) -> None:
    store = DriveMediaStore(FakeDownloader({}), tmp_path)
    with pytest.raises(Rejected, match="was not found in the Drive folder"):
        store.ensure_local([item(file_id=None)])


def test_several_files_come_back_in_order(tmp_path: Path) -> None:
    store = DriveMediaStore(FakeDownloader({"F1": VIDEO, "F2": b"two"}), tmp_path)
    renditions = store.ensure_local([item(), item("b.jpg", "F2", b"two")])
    assert [Path(r.path).name.split("-", 1)[1] for r in renditions] == ["eid.mp4", "b.jpg"]
