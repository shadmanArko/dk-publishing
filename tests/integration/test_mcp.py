"""The MCP endpoint (ADR 0024): the same use cases and answers as the HTTP API, gated by the same keys.

In-process tests drive the server through fastmcp's own client with a chosen key; one test goes
through the real HTTP mount with a Bearer key, as an agent would.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError

from dk_publishing.application.ports import ApiKey
from dk_publishing.application.use_cases import api_keys
from dk_publishing.entrypoints.publish_api.app import create_app
from dk_publishing.entrypoints.publish_mcp.server import DRAFT_TOOLS, READ_TOOLS, build_mcp
from tests.integration.conftest import TENANT
from tests.integration.test_api import Rig
from tests.integration.test_batch_api import text_post
from tests.integration.test_status_api import sheet_post
from tests.support import T0


@pytest.fixture
def rig(conninfo: str, tmp_path: Path) -> Rig:
    rig = Rig(conninfo, tmp_path)
    sheet_post(conninfo, "S-SCHEDULED", "facebook", "approved", T0.replace(year=T0.year + 1))
    return rig


def as_key(rig: Rig, right: str, **kw: Any) -> Any:
    key = ApiKey("id", TENANT, f"{right}-key", (right,))
    return build_mcp(rig.api, who=lambda: key, auth=False, **kw)


def call(server: Any, tool: str, **args: Any) -> Any:
    async def go() -> Any:
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            return result.structured_content

    return asyncio.run(go())


def names(server: Any) -> set[str]:
    async def go() -> set[str]:
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    return asyncio.run(go())


def test_a_key_sees_only_what_its_right_covers_and_nobody_can_approve(rig: Rig) -> None:
    assert names(as_key(rig, "read")) == READ_TOOLS
    assert names(as_key(rig, "draft")) == READ_TOOLS | DRAFT_TOOLS
    assert names(as_key(rig, "approve")) == READ_TOOLS | DRAFT_TOOLS  # no approve tool, ever
    with pytest.raises(ToolError, match="forbidden"):
        call(as_key(rig, "read"), "submit_posts", posts=[text_post("x")])


def test_a_switched_off_tool_is_gone_and_refused(rig: Rig) -> None:
    server = as_key(rig, "draft", disabled=frozenset({"submit_posts"}))
    assert "submit_posts" not in names(server)
    with pytest.raises(ToolError, match="switched_off"):
        call(server, "submit_posts", posts=[text_post("x")])


def test_reads_answer_exactly_what_the_http_api_answers(rig: Rig) -> None:
    server = as_key(rig, "read")
    assert call(server, "publishing_overview") == rig.call("GET", "/v1/overview", "read").json()
    http = rig.call("GET", "/v1/calendar?limit=20", "read").json()
    assert call(server, "list_posts") == http
    spec = rig.call("GET", "/v1/spec?platform=instagram&format=reel", "read").json()
    assert call(server, "publishing_spec", platform="instagram", format="reel") == spec
    seen = call(server, "get_post", post_key="S-SCHEDULED")
    assert seen == rig.call("GET", "/v1/posts/S-SCHEDULED", "read").json()


def test_submitting_checks_then_creates_drafts_safely(rig: Rig) -> None:
    server = as_key(rig, "draft")
    posts = [text_post("m-1"), text_post("m-bad", caption="")]
    checked = call(server, "submit_posts", posts=posts, dry_run=True)
    assert (checked["ok"], checked["failed"]) == (1, 1) and checked["dry_run"] is True
    created = call(server, "submit_posts", posts=posts)
    assert created["results"][0]["post"]["summary"] == "awaiting_approval"
    again = call(server, "submit_posts", posts=posts)
    assert again["results"][0]["replayed"] is True
    on_calendar = rig.call("GET", "/v1/calendar?source=api", "read").json()["posts"]
    assert [p["post_key"] for p in on_calendar] == ["m-1"] and on_calendar[0]["by"] == "draft-key"


def test_cancelling_works_on_own_posts_only_and_errors_teach(rig: Rig) -> None:
    server = as_key(rig, "draft")
    call(server, "submit_posts", posts=[text_post("m-cancel")])
    assert call(server, "cancel_post", post_key="m-cancel")["summary"] == "cancelled"
    with pytest.raises(ToolError, match=r"\[conflict\].*Google Sheet"):
        call(server, "cancel_post", post_key="S-SCHEDULED")
    with pytest.raises(ToolError, match=r"\[not_found\]"):
        call(as_key(rig, "read"), "publishing_spec", platform="nowhere")


def test_the_contract_and_guide_are_resources_and_the_prompt_lists_the_steps(rig: Rig) -> None:
    async def go() -> tuple[Any, Any, Any]:
        async with Client(as_key(rig, "read")) as client:
            spec = await client.read_resource("dkp://spec")
            guide = await client.read_resource("dkp://guide")
            prompt = await client.get_prompt("publish_posts", {"platform": "instagram"})
            return spec, guide, prompt

    spec, guide, prompt = asyncio.run(go())
    assert len(json.loads(spec[0].text)["spec_version"]) == 16
    assert "POST /v1/posts" in guide[0].text
    text = prompt.messages[0].content.text
    assert "dry_run=true" in text and "checking" in text


def test_an_agent_connects_over_http_with_its_key(rig: Rig) -> None:
    endpoint = build_mcp(rig.api).http_app(path="/", stateless_http=True, json_response=True)
    accept = {"Accept": "application/json, text/event-stream"}
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    with TestClient(create_app(rig.api, endpoint)) as client:
        assert client.post("/mcp/", json=listing, headers=accept).status_code == 401
        bad = {**accept, "Authorization": "Bearer dkp_nonsense"}
        assert client.post("/mcp/", json=listing, headers=bad).status_code == 401
        good = {**accept, "Authorization": f"Bearer {rig.keys['read']}"}
        answer = client.post("/mcp/", json=listing, headers=good)
        assert answer.status_code == 200, answer.text
        assert {t["name"] for t in answer.json()["result"]["tools"]} == READ_TOOLS
        revoked = api_keys.revoke_key(rig.api.core, TENANT, "read-key")
        assert revoked and client.post("/mcp/", json=listing, headers=good).status_code == 401
        assert client.get("/healthz").json() == {"status": "ok"}  # the API itself is unchanged
