from __future__ import annotations

from pathlib import Path

import pytest

from dk_publishing import composition
from dk_publishing.adapters.sheets.google_access import CredentialsError
from dk_publishing.adapters.sheets.sheet_init import InitReport
from dk_publishing.entrypoints import cli


def env(monkeypatch: pytest.MonkeyPatch, *, complete: bool = True) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)
    if complete:
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "~/key.json")
        monkeypatch.setenv("GOOGLE_SHEET_ID", "SID")


def fake_init(report: InitReport, seen: list[bool]):  # type: ignore[no-untyped-def]
    def init_sheet(path: Path, sheet_id: str, *, dry_run: bool) -> InitReport:
        seen.append(dry_run)
        assert sheet_id == "SID" and path.name == "key.json"
        return report

    return init_sheet


def test_needs_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env(monkeypatch, complete=False)
    assert cli.main(["sheet", "init"]) == 2
    assert "GOOGLE_SHEET_ID" in capsys.readouterr().err


def test_a_dry_run_says_what_would_happen_and_that_nothing_was_written(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env(monkeypatch)
    seen: list[bool] = []
    report = InitReport(
        dry_run=True,
        timezone_set="Europe/Berlin",
        renamed=[("Sheet1", "README")],
        created=["Posts", "Instagram"],
        headers_added={"Posts": ["post_key"], "Instagram": ["post_key"], "README": []},
        formatted=["Posts", "Instagram"],
        left_alone=["My budget"],
        readme_written=True,
    )
    monkeypatch.setattr(composition, "init_sheet", fake_init(report, seen))

    assert cli.main(["sheet", "init", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert seen == [True]
    assert "[would] set the Sheet time zone to Europe/Berlin" in out
    assert "[would] rename the blank tab 'Sheet1' to 'README'" in out
    assert "[would] create 2 tabs: Posts, Instagram" in out
    assert "[keep] left alone (not ours): My budget" in out
    assert "Dry run: nothing was written" in out
    assert "add headers" not in out  # headers of tabs being created are not listed twice


def test_a_real_run_uses_past_tense_and_exits_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env(monkeypatch)
    seen: list[bool] = []
    report = InitReport(dry_run=False, headers_added={"Posts": ["ready"]}, formatted=["Posts"])
    monkeypatch.setattr(composition, "init_sheet", fake_init(report, seen))
    assert cli.main(["sheet", "init"]) == 0
    out = capsys.readouterr().out
    assert seen == [False] and "[did] add headers to 'Posts': ready" in out
    assert "Dry run" not in out


def test_problems_make_the_exit_code_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env(monkeypatch)
    report = InitReport(
        dry_run=False, problems=["Tab 'Posts': the header row has the same name twice."]
    )
    monkeypatch.setattr(composition, "init_sheet", fake_init(report, []))
    assert cli.main(["sheet", "init"]) == 1
    assert "[FAIL] Tab 'Posts'" in capsys.readouterr().out


def test_a_bad_key_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env(monkeypatch)

    def boom(*_: object, **__: object) -> InitReport:
        raise CredentialsError("no key file at /nowhere")

    monkeypatch.setattr(composition, "init_sheet", boom)
    assert cli.main(["sheet", "init"]) == 1
    assert "[FAIL] no key file" in capsys.readouterr().err
