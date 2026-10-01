from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from dk_publishing import composition
from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.application.ports import DuplicateAccount
from tests.unit.test_gateway import TABS, FakeGoogle, blank


def test_accounts_can_be_added_listed_and_are_unique_per_platform(conninfo: str) -> None:
    composition.add_account(conninfo, "instagram", "Dhaka Kacchi (dry run)")
    composition.add_account(conninfo, "facebook", "Dhaka Kacchi (dry run)")  # other platform: fine
    with pytest.raises(DuplicateAccount):
        composition.add_account(conninfo, "instagram", "dhaka kacchi (DRY run)")  # case-insensitive
    accounts = composition.list_accounts(conninfo)
    assert [(a.platform, a.display_name, a.status) for a in accounts] == [
        ("facebook", "Dhaka Kacchi (dry run)", "active"),
        ("instagram", "Dhaka Kacchi (dry run)", "active"),
    ]


def test_an_account_for_an_unknown_platform_is_refused(conninfo: str) -> None:
    with pytest.raises(ConfigError, match="unknown platform"):
        composition.add_account(conninfo, "myspace", "Tom")


def install(conninfo: str, fake: FakeGoogle, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(composition, "connect", lambda path: (fake, None, "sa@example", None))
    return composition.install_sample(conninfo, Path("key.json"), "SID", "FOLDER")


def col(tab: str, name: str) -> str:
    return chr(65 + TABS[tab].index(name))


def cells(fake: FakeGoogle) -> dict[str, Any]:
    return {u["range"]: u["values"][0][0] for u in fake.updates}


def test_the_sample_post_and_its_dry_run_accounts_are_installed_once(
    conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGoogle(blank())
    done = install(conninfo, fake, monkeypatch)

    assert any("added DK-2026-0412 to the Posts tab" in line for line in done)
    posts = TABS["Posts"]
    written = cells(fake)
    assert written[f"'Posts'!{col('Posts', 'post_key')}2"] == "DK-2026-0412"
    assert written[f"'Posts'!{col('Posts', 'ready')}2"] is False  # left for you to tick
    slot = written[f"'Posts'!{col('Posts', 'default_slot')}2"]
    assert isinstance(slot, float) and slot == pytest.approx(46340.75)  # 14.11.2026 18:00
    assert written[f"'Instagram'!{col('Instagram', 'format')}2"] == "reel"
    assert written[f"'Facebook'!{col('Facebook', 'account')}2"] == "Dhaka Kacchi (dry run)"
    assert {a.platform for a in composition.list_accounts(conninfo)} == {"instagram", "facebook"}
    assert posts  # the Posts header exists

    # running it again must not add a second copy of the post
    fake2 = FakeGoogle(blank())
    fake2.grids["Posts"].append([None] * len(TABS["Posts"]))
    fake2.grids["Posts"][1][TABS["Posts"].index("post_key")] = "DK-2026-0412"
    again = install(conninfo, fake2, monkeypatch)
    assert fake2.updates == [] and any("already on the Posts tab" in line for line in again)


def test_a_sheet_with_a_broken_tab_is_not_written_to(
    conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    grids = blank()
    grids["Instagram"][0].pop(0)  # lost its post_key header
    fake = FakeGoogle(grids)
    with pytest.raises(ConfigError, match="missing column"):
        install(conninfo, fake, monkeypatch)
    assert fake.updates == []


def test_the_sample_dates_parse_the_way_the_readme_promises() -> None:
    from dk_publishing.domain.timezones import serial_to_local

    assert serial_to_local(46340.75) == datetime(2026, 11, 14, 18, 0)
