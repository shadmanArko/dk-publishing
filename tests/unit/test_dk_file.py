"""The single dk.json: one file, many sections, every secret in one place."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from dk_publishing import composition
from dk_publishing.adapters.config.platforms import ConfigError, load_platforms
from dk_publishing.adapters.config.secrets_file import SecretsFile, make_ref, split_ref
from dk_publishing.adapters.notify.telegram import TelegramCredentials, run_setup
from dk_publishing.adapters.platforms.connect import connect_platform
from dk_publishing.adapters.platforms.dk_file import TEMPLATE, create_template, resolve_env
from dk_publishing.adapters.platforms.meta_credentials import MetaCredentials
from dk_publishing.adapters.platforms.setup_check import check_setup
from dk_publishing.adapters.platforms.youtube_api import YouTubeCredentials
from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG


@pytest.fixture
def dk(tmp_path: Path) -> Path:
    path = tmp_path / "conf" / "dk.json"
    assert create_template(path)
    return path


def fill(path: Path, **sections: dict[str, object]) -> None:
    data = json.loads(path.read_text())
    for name, values in sections.items():
        data[name].update(values)
    path.write_text(json.dumps(data))


def test_the_template_is_private_complete_and_never_overwritten(dk: Path) -> None:
    assert stat.S_IMODE(dk.stat().st_mode) == 0o600
    assert {"google", "meta", "youtube", "telegram", "alerts", "media"} <= set(
        json.loads(dk.read_text())
    )
    dk.write_text('{"mine": 1}')
    assert not create_template(dk) and dk.read_text() == '{"mine": 1}'


def test_every_secret_the_system_uses_has_a_home_in_the_template() -> None:
    meta, youtube = TEMPLATE["meta"], TEMPLATE["youtube"]
    assert {"page_id", "access_token"} <= set(meta["facebook"])
    assert {"account_id", "access_token"} <= set(meta["instagram"])
    assert {"user_id", "access_token"} <= set(meta["threads"])
    assert {"client_id", "client_secret", "refresh_token"} <= set(youtube)
    assert {"bot_token", "chat_id"} <= set(TEMPLATE["telegram"])


def test_references_split_into_a_file_and_a_section() -> None:
    assert split_ref("/a/b.json#meta") == (Path("/a/b.json"), "meta")
    assert split_ref("/a/b.json") == (Path("/a/b.json"), None)
    assert split_ref(make_ref(Path("/a/b.json"), "x")) == (Path("/a/b.json"), "x")


def test_without_a_config_file_the_environment_is_unchanged() -> None:
    assert resolve_env({"A": "1"}) == {"A": "1"}


def test_the_file_fills_in_every_name_the_code_reads(dk: Path) -> None:
    fill(
        dk,
        google={"sheet_id": "SHEET", "drive_folder_id": "FOLDER"},
        alerts={"heartbeat_url": "https://hc/ping"},
        media={"public_base_url": "https://m.example", "public_dir": "/srv/m"},
    )
    env = resolve_env({"DK_CONFIG_FILE": str(dk), "DATABASE_URL": "db"})
    assert env["GOOGLE_SHEET_ID"] == "SHEET" and env["GOOGLE_DRIVE_FOLDER_ID"] == "FOLDER"
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == str(dk.parent / "google-key.json")
    assert env["HEARTBEAT_URL"] == "https://hc/ping" and env["PUBLIC_MEDIA_DIR"] == "/srv/m"
    assert env["PUBLIC_MEDIA_BASE_URL"] == "https://m.example" and env["DATABASE_URL"] == "db"
    for name, section in (
        ("META_CREDENTIALS_FILE", "meta"),
        ("YOUTUBE_CREDENTIALS_FILE", "youtube"),
        ("TELEGRAM_CREDENTIALS_FILE", "telegram"),
    ):
        assert env[name] == f"{dk}#{section}"


def test_an_explicit_environment_variable_beats_the_file(dk: Path) -> None:
    fill(dk, google={"sheet_id": "FROM_FILE"})
    env = resolve_env({"DK_CONFIG_FILE": str(dk), "GOOGLE_SHEET_ID": "FROM_ENV"})
    assert env["GOOGLE_SHEET_ID"] == "FROM_ENV"


def test_a_missing_or_broken_file_is_explained(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="make init"):
        resolve_env({"DK_CONFIG_FILE": str(tmp_path / "none.json")})
    bad = tmp_path / "bad.json"
    bad.write_text('{"google": {\n  "sheet_id": oops}')
    with pytest.raises(ConfigError, match=r"not valid JSON \(line 2, column"):
        resolve_env({"DK_CONFIG_FILE": str(bad)})
    bad.write_text("[]")
    with pytest.raises(ConfigError, match="JSON object"):
        resolve_env({"DK_CONFIG_FILE": str(bad)})


def test_each_credentials_class_reads_its_own_section(dk: Path) -> None:
    fill(
        dk,
        meta={"facebook": {"page_id": "P", "access_token": "T"}},
        youtube={"client_id": "cid", "client_secret": "sec", "refresh_token": "rt"},
        telegram={"bot_token": "123:abc", "chat_id": "7"},
    )
    assert MetaCredentials(f"{dk}#meta").section("facebook")["page_id"] == "P"
    assert YouTubeCredentials(f"{dk}#youtube").load()["client_id"] == "cid"
    assert TelegramCredentials(f"{dk}#telegram").load()["chat_id"] == "7"
    assert MetaCredentials(f"{dk}#meta").path == dk


def test_writing_a_section_keeps_everything_else_and_the_permissions(dk: Path) -> None:
    fill(dk, google={"sheet_id": "KEEP"}, telegram={"bot_token": "123:abc"})
    TelegramCredentials(f"{dk}#telegram").update({"chat_id": "42"})
    YouTubeCredentials(f"{dk}#youtube").update({"refresh_token": "new"})
    MetaCredentials(f"{dk}#meta").update_section("threads", {"access_token": "renewed"})
    data = json.loads(dk.read_text())
    assert data["telegram"] == {**data["telegram"], "bot_token": "123:abc", "chat_id": "42"}
    assert data["google"]["sheet_id"] == "KEEP" and data["youtube"]["refresh_token"] == "new"
    assert data["meta"]["threads"]["access_token"] == "renewed"
    assert data["meta"]["facebook"] == {"page_id": "", "access_token": ""}  # untouched sibling
    assert stat.S_IMODE(dk.stat().st_mode) == 0o600 and not list(dk.parent.glob("*.tmp"))


def test_a_missing_section_says_to_run_init(dk: Path) -> None:
    data = json.loads(dk.read_text())
    del data["telegram"]
    dk.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="no 'telegram' section; run `dk init`"):
        TelegramCredentials(f"{dk}#telegram").load()
    with pytest.raises(ConfigError, match="no 'telegram' section"):
        SecretsFile(f"{dk}#telegram").update({"a": 1})


def test_setup_commands_never_create_stray_files_when_the_file_is_dk_json(dk: Path) -> None:
    before = sorted(p.name for p in dk.parent.iterdir())
    init = run_setup("init", f"{dk}#telegram")
    assert init.ok and "telegram.bot_token" in init.lines[0]
    assert not composition.meta_init(f"{dk}#meta", {})
    login = connect_platform("youtube", f"{dk}#youtube")
    assert not login.ok and "client_id" in login.lines[0]
    assert sorted(p.name for p in dk.parent.iterdir()) == before


def test_check_setup_without_a_config_file_says_what_to_do() -> None:
    report = check_setup({}, load_platforms(DEFAULT_PLATFORMS_CONFIG))
    assert report.problems == 1 and "make init" in report.lines[0]


def test_check_setup_on_an_empty_template_skips_optional_parts_and_fails_google(dk: Path) -> None:
    env = resolve_env({"DK_CONFIG_FILE": str(dk)})
    report = check_setup(env, load_platforms(DEFAULT_PLATFORMS_CONFIG))
    text = "\n".join(report.lines)
    assert "[FAIL] google: fill in sheet_id, drive_folder_id" in text
    assert "[skip] meta" in text and "[skip] youtube" in text and "[skip] telegram" in text
    assert "[skip] media" in text and "platforms switched on" in text
    assert report.problems == 1  # only Google is required


def test_check_setup_reports_a_broken_config_file(tmp_path: Path) -> None:
    bad = tmp_path / "dk.json"
    bad.write_text("{")
    report = check_setup({"DK_CONFIG_FILE": str(bad)}, load_platforms(DEFAULT_PLATFORMS_CONFIG))
    assert report.problems == 1 and "not valid JSON" in report.lines[0]


def test_init_config_creates_once_and_names_the_path(tmp_path: Path) -> None:
    target = tmp_path / "x" / "dk.json"
    assert composition.init_config({"DK_CONFIG_FILE": str(target)}) == (target, True)
    assert composition.init_config({"DK_CONFIG_FILE": str(target)}) == (target, False)


def test_init_works_when_dk_config_file_already_names_the_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dk_publishing.entrypoints.cli import main

    target = tmp_path / "new" / "dk.json"
    monkeypatch.setenv("DK_CONFIG_FILE", str(target))
    assert main(["init"]) == 0 and target.exists()
    assert "created" in capsys.readouterr().out
    assert main(["init"]) == 0 and "already exists" in capsys.readouterr().out


def test_accounts_are_derived_from_the_ids_in_dk_json(dk: Path) -> None:
    from dk_publishing.adapters.platforms.dk_file import configured_accounts

    env = {"DK_CONFIG_FILE": str(dk)}
    assert configured_accounts(env) == []  # the empty template has no ids
    fill(
        dk,
        meta={
            "facebook": {"page_id": "PAGE"},
            "instagram": {"account_id": " IG "},
            "threads": {"user_id": ""},
        },
        youtube={"channel_id": "UC1"},
    )
    assert configured_accounts(env) == [
        ("facebook", "PAGE"),
        ("instagram", "IG"),
        ("youtube", "UC1"),
    ]
    assert configured_accounts({}) == []
