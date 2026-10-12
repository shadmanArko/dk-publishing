"""GET /v1/overview and /v1/calendar: where publishing stands, for the Sheet's posts and the API's."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from tests.integration.conftest import TENANT
from tests.integration.test_api import Rig
from tests.support import T0


@pytest.fixture
def rig(conninfo: str, tmp_path: Path) -> Rig:
    return Rig(conninfo, tmp_path)


def sheet_post(
    conninfo: str, key: str, platform: str, status: str, slot: datetime, **extra: str
) -> None:
    """A post made in the Google Sheet, in a given state (the Sheet sync's own rows)."""
    content = {"format": extra.pop("format", "post"), "caption": f"{key} caption"}
    with psycopg.connect(conninfo) as conn:
        row = conn.execute(
            "INSERT INTO publishing.posts (tenant_id, post_key) VALUES (%s, %s) RETURNING id",
            (TENANT, key),
        ).fetchone()
        account = conn.execute(
            "SELECT id FROM publishing.social_accounts WHERE tenant_id = %s AND platform = %s",
            (TENANT, platform),
        ).fetchone()
        assert row and account
        frozen = status not in (
            "draft",
            "invalid",
            "pending_approval",
            "failed",
            "cancelled",
            "expired",
        )
        conn.execute(
            """INSERT INTO publishing.variants (tenant_id, post_id, platform, account_id, status,
                   publish_at, snapshot, snapshot_hash, published_at, external_url, external_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                TENANT, row[0], platform, account[0], status, slot,
                json.dumps(content) if frozen else None, "h" if frozen else None,
                slot if status == "published" else None,
                extra.get("url"), extra.get("external_id"),
            ),
        )  # fmt: skip


@pytest.fixture
def week(rig: Rig, conninfo: str) -> Rig:
    sheet_post(conninfo, "S-PUBLISHED", "instagram", "published", T0 - timedelta(days=2),
               url="https://instagram.example/p/1", format="reel")  # fmt: skip
    sheet_post(conninfo, "S-SCHEDULED", "facebook", "approved", T0 + timedelta(days=3))
    sheet_post(conninfo, "S-FAILED", "youtube", "failed", T0 - timedelta(days=1), format="video")
    sheet_post(conninfo, "S-OLD", "threads", "published", T0 - timedelta(days=30),
               url="https://threads.example/1", format="text")  # fmt: skip
    assert rig.post(rig.reel("A-DRAFT")).status_code == 201  # an agent's draft, 2 days ahead
    return rig


def test_the_overview_counts_every_post_the_sheets_too(week: Rig) -> None:
    o = week.call("GET", "/v1/overview", "read").json()
    assert o["pipeline"]["total"] == 2
    assert o["pipeline"]["by_state"] == {
        "awaiting_approval": {"instagram": 1}, "scheduled": {"facebook": 1},
    }  # fmt: skip
    assert o["published"] == {
        "total": 1,
        "by_platform": {"instagram": 1},
    }  # S-OLD is outside 7 days
    assert [p["post_key"] for p in o["problems"]] == ["S-FAILED"]
    assert [p["post_key"] for p in o["next"]] == ["A-DRAFT", "S-SCHEDULED"]
    (waiting,) = o["awaiting_approval"]
    assert (
        waiting["post_key"] == "A-DRAFT"
        and waiting["by"] == "draft-key"
        and waiting["source"] == "api"
    )
    assert o["plan_ends"].startswith((T0 + timedelta(days=3)).date().isoformat())
    assert "Nothing is scheduled after" in o["note"]
    wide = week.call("GET", "/v1/overview?days_back=60", "read").json()
    assert wide["published"]["total"] == 2


def test_the_calendar_lists_and_narrows_every_post(week: Rig) -> None:
    every = week.call("GET", "/v1/calendar", "read").json()
    assert every["total"] == 5
    slots = [p["slot"] for p in every["posts"]]
    assert slots == sorted(slots)  # by slot
    sheet = week.call("GET", "/v1/calendar?source=sheet", "read").json()
    assert sheet["total"] == 4 and all(p["source"] == "sheet" for p in sheet["posts"])
    history = week.call("GET", "/v1/calendar?state=published", "read").json()["posts"]
    assert [p["post_key"] for p in history] == ["S-PUBLISHED", "S-OLD"]  # newest first
    assert (
        history[0]["live_url"] == "https://instagram.example/p/1" and history[0]["format"] == "reel"
    )
    only = week.call("GET", "/v1/calendar?platform=facebook", "read").json()["posts"]
    assert [p["post_key"] for p in only] == ["S-SCHEDULED"]
    start = (T0 - timedelta(days=3)).isoformat()
    window = week.call("GET", "/v1/calendar", "read", params={"from": start, "to": T0.isoformat()})
    assert [p["post_key"] for p in window.json()["posts"]] == ["S-PUBLISHED", "S-FAILED"]
    assert week.call("GET", "/v1/calendar?limit=2", "read").json()["limit"] == 2


def test_bad_filters_say_what_is_allowed(week: Rig) -> None:
    bad_state = week.call("GET", "/v1/calendar?state=done", "read")
    assert bad_state.status_code == 422 and "published" in bad_state.json()["detail"]
    assert week.call("GET", "/v1/calendar?source=email", "read").status_code == 422
    assert week.call("GET", "/v1/calendar?from=yesterday", "read").status_code == 422
    assert week.client.get("/v1/overview").status_code == 401


def test_a_sheet_post_can_be_read_but_not_changed(week: Rig) -> None:
    seen = week.call("GET", "/v1/posts/S-PUBLISHED", "read").json()
    assert seen["summary"] == "published"
    assert seen["variants"][0]["live_url"] == "https://instagram.example/p/1"
    assert seen["variants"][0]["format"] == "reel"
    assert week.call("DELETE", "/v1/posts/S-SCHEDULED").status_code == 409
    assert week.call("POST", "/v1/posts/S-SCHEDULED/approve", "approve").status_code == 409
