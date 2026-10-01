from __future__ import annotations

import pytest

from dk_publishing import composition
from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.application.ports import AccountInfo, DuplicateAccount
from dk_publishing.application.use_cases.sync_sheet import SyncReport
from dk_publishing.entrypoints import cli

NAMES = (
    "DATABASE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_SHEET_ID",
    "GOOGLE_DRIVE_FOLDER_ID",
)


def env(monkeypatch: pytest.MonkeyPatch, *, skip: str | None = None) -> None:
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
        if name != skip:
            monkeypatch.setenv(name, f"~/{name}.json" if "CREDENTIALS" in name else name)


def fake_sync(monkeypatch: pytest.MonkeyPatch, report: SyncReport, seen: list[bool]) -> None:
    monkeypatch.setattr(composition, "build_sync_services", lambda *a, **k: object())

    def run(services: object, *, allow_cancellations: bool) -> SyncReport:
        seen.append(allow_cancellations)
        return report

    monkeypatch.setattr(cli, "sync_sheet", run)


@pytest.mark.parametrize("command", [["sync"], ["sheet", "sample"]])
def test_commands_that_need_google_say_what_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    env(monkeypatch, skip="GOOGLE_SHEET_ID")
    assert cli.main(command) == 2
    assert "GOOGLE_SHEET_ID" in capsys.readouterr().err


def test_a_clean_sync_prints_a_summary_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env(monkeypatch)
    seen: list[bool] = []
    fake_sync(
        monkeypatch,
        SyncReport(created=2, approved=2, cells_written=14, problems=["Tab 'X' is odd."]),
        seen,
    )
    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert (
        "created 2, approved 2" in out
        and "wrote 14 cells" in out
        and "[warn] Tab 'X' is odd." in out
    )
    assert seen == [False]


def test_the_override_flag_reaches_the_use_case(monkeypatch: pytest.MonkeyPatch) -> None:
    env(monkeypatch)
    seen: list[bool] = []
    fake_sync(monkeypatch, SyncReport(), seen)
    cli.main(["sync", "--allow-cancellations"])
    assert seen == [True]


def test_a_halted_sync_is_loud_and_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env(monkeypatch)
    fake_sync(
        monkeypatch,
        SyncReport(halted="This sync would cancel 6 scheduled posts. Nothing was changed."),
        [],
    )
    assert cli.main(["sync"]) == 1
    assert "[HALTED] This sync would cancel 6" in capsys.readouterr().out


def test_variant_errors_fail_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env(monkeypatch)
    fake_sync(monkeypatch, SyncReport(errors=["DK-1/instagram: RuntimeError: boom"]), [])
    assert cli.main(["sync"]) == 1
    assert "[FAIL] DK-1/instagram" in capsys.readouterr().out


def test_account_add_and_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env(monkeypatch)
    added: list[tuple[str, str]] = []

    def record(url: str, platform: str, name: str, external_id: str | None = None) -> str:
        added.append((platform, name))
        return "id"

    monkeypatch.setattr(composition, "add_account", record)
    assert cli.main(["account", "add", "instagram", "Dhaka Kacchi (dry run)"]) == 0
    assert added == [("instagram", "Dhaka Kacchi (dry run)")]
    assert "appears in the Sheet's account dropdown" in capsys.readouterr().out

    monkeypatch.setattr(
        composition,
        "list_accounts",
        lambda url: [AccountInfo("1", "instagram", "Dhaka Kacchi", "active")],
    )
    assert cli.main(["account", "list"]) == 0
    assert "instagram" in capsys.readouterr().out


@pytest.mark.parametrize(
    "error", [ConfigError("unknown platform 'x'"), DuplicateAccount("instagram: A")]
)
def test_account_add_failures_are_readable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    env(monkeypatch)

    def boom(*_: object, **__: object) -> str:
        raise error

    monkeypatch.setattr(composition, "add_account", boom)
    assert cli.main(["account", "add", "x", "A"]) == 1
    assert "[FAIL]" in capsys.readouterr().err


def test_sample_reports_what_it_did(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env(monkeypatch)
    monkeypatch.setattr(
        composition, "install_sample", lambda *a: ["added DK-2026-0412 to the Posts tab"]
    )
    assert cli.main(["sheet", "sample"]) == 0
    assert "[did] added DK-2026-0412" in capsys.readouterr().out
