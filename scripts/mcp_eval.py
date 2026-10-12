"""Measure the publishing contract, batches and MCP endpoint end to end (ADR 0024).

    uv run python scripts/mcp_eval.py --database-url postgresql://USER:PASS@127.0.0.1:PORT/postgres

What it does, on a throwaway database it creates and drops (nothing reaches a real platform):
- builds the API and MCP endpoint over the integration tests' rig: the REAL platform adapters
  (real rules) talking to fake platforms, and serves them on a free local port;
- seeds a few Google Sheet posts (published, scheduled, failed), makes a read and a draft key;
- CODE PATH: hands over a batch of posts with plain HTTP (dry run, then for real): 0 AI tokens;
- AI PATH (unless --skip-ai): headless Claude Code against the MCP endpoint on six typical jobs,
  each checked against the database; records tool calls, tokens and time.

Needs the dev dependencies (it imports the test rig) and, for the AI path, a signed-in `claude` CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
import uvicorn
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.integration.test_status_api import sheet_post  # noqa: E402
from tests.support import FakeClock  # noqa: E402
from tests.support.api_rig import api_services  # noqa: E402
from tests.support.fake_telegram import FakeNotifier  # noqa: E402

from dk_publishing.adapters.persistence.migrate import apply_migrations  # noqa: E402
from dk_publishing.application.use_cases import api_keys  # noqa: E402
from dk_publishing.entrypoints.publish_api.app import create_app  # noqa: E402
from dk_publishing.entrypoints.publish_mcp.server import build_mcp  # noqa: E402

TENANT = "dk"
BERLIN = ZoneInfo("Europe/Berlin")
PLATFORMS = ("instagram", "facebook", "threads", "youtube", "tiktok")
SYSTEM = (
    "You are an assistant for a small food business's social media. Use the dk_publish tools only. "
    "Be brief. When asked for JSON, answer with ONLY that JSON."
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def slot(days: int, hour: int) -> str:
    day = datetime.now(BERLIN).date() + timedelta(days=days)
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=BERLIN).isoformat()


# --- the stack ---------------------------------------------------------------------------------


class Stack:
    def __init__(self, admin_url: str) -> None:
        self.admin_url = admin_url
        self.name = f"dkpub_eval_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.name)))
        self.url = psycopg.conninfo.make_conninfo(admin_url, dbname=self.name)
        apply_migrations(self.url, ROOT / "migrations")
        self.tmp = Path(tempfile.mkdtemp(prefix="dkpub-eval-"))
        self.api = api_services(self.url, self.tmp, FakeClock(datetime.now(UTC)), FakeNotifier())
        with self.api.core.uow() as uow:
            for platform in PLATFORMS:
                uow.sync.add_account(TENANT, platform, "Dhaka Kacchi", uuid.uuid4().hex)
            uow.commit()
        now = datetime.now(UTC)
        self.seed = {
            "published_url": "https://www.instagram.example/p/EVAL1",
            "plan_ends": (now + timedelta(days=3)).replace(microsecond=0),
        }
        sheet_post(
            self.url,
            "S-REEL",
            "instagram",
            "published",
            now - timedelta(days=2),
            url=self.seed["published_url"],
            format="reel",
        )
        sheet_post(self.url, "S-OFFER", "facebook", "approved", self.seed["plan_ends"])
        sheet_post(
            self.url, "S-SHORT", "youtube", "failed", now - timedelta(days=1), format="video"
        )
        self.read_key = api_keys.create_key(self.api.core, TENANT, "eval-read", "read")
        self.draft_key = api_keys.create_key(self.api.core, TENANT, "eval-draft", "draft")
        endpoint = build_mcp(self.api).http_app(path="/", stateless_http=True, json_response=True)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(
            create_app(self.api, endpoint), "127.0.0.1", self.port, log_level="error"
        )
        self.server = uvicorn.Server(config)
        threading.Thread(target=self.server.run, daemon=True).start()
        while not self.server.started:
            time.sleep(0.05)

    def http(self, key: str) -> httpx.Client:
        return httpx.Client(
            base_url=self.base, headers={"Authorization": f"Bearer {key}"}, timeout=60
        )

    def post(self, post_key: str) -> dict[str, Any] | None:
        with self.http(self.read_key) as c:
            answer = c.get(f"/v1/posts/{post_key}")
        return answer.json() if answer.status_code == 200 else None

    def close(self) -> None:
        self.server.should_exit = True
        time.sleep(0.3)
        shutil.rmtree(self.tmp, ignore_errors=True)
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(self.name))
            )


# --- the code path: plain HTTP, no AI ------------------------------------------------------------


def code_path(stack: Stack) -> dict[str, Any]:
    started = time.monotonic()
    with stack.http(stack.draft_key) as c:
        reel = c.post("/v1/media", params={"filename": "reel.mp4"}, content=b"reel-bytes").json()[
            "media_id"
        ]
        photo = c.post("/v1/media", params={"filename": "plate.jpg"}, content=b"jpeg").json()[
            "media_id"
        ]
        video = c.post("/v1/media", params={"filename": "clip.mp4"}, content=b"clip").json()[
            "media_id"
        ]
        posts = [
            {
                "post_key": "wk-ig-reel",
                "variants": [
                    {
                        "platform": "instagram",
                        "format": "reel",
                        "caption": "Slow-cooked kacchi, Berlin.",
                        "media": [reel],
                        "slot": slot(4, 18),
                        "options": {"share_to_feed": True},
                    }
                ],
            },
            {
                "post_key": "wk-ig-feed",
                "variants": [
                    {
                        "platform": "instagram",
                        "format": "feed",
                        "caption": "Saturday plates.",
                        "media": [photo],
                        "slot": slot(5, 12),
                    }
                ],
            },
            {
                "post_key": "wk-fb-text",
                "variants": [
                    {
                        "platform": "facebook",
                        "format": "post",
                        "caption": "Order your Saturday kacchi.",
                        "slot": slot(4, 9),
                    }
                ],
            },
            {
                "post_key": "wk-th-text",
                "variants": [
                    {
                        "platform": "threads",
                        "format": "text",
                        "caption": "Kacchi o'clock in Berlin.",
                        "slot": slot(5, 19),
                    }
                ],
            },
            {
                "post_key": "wk-yt-video",
                "variants": [
                    {
                        "platform": "youtube",
                        "format": "video",
                        "media": [video],
                        "slot": slot(6, 17),
                        "options": {"title": "Opening the dum pot", "made_for_kids": "no"},
                    }
                ],
            },
            {
                "post_key": "wk-tt-video",
                "variants": [
                    {
                        "platform": "tiktok",
                        "format": "video",
                        "caption": "Dum time.",
                        "media": [video],
                        "slot": slot(6, 20),
                        "options": {"privacy_level": "public"},
                    }
                ],
            },
        ]
        checked = c.post("/v1/posts/batch", json={"posts": posts, "dry_run": True}).json()
        created = c.post("/v1/posts/batch", json={"posts": posts}).json()
        again = c.post("/v1/posts/batch", json={"posts": posts}).json()
    return {
        "posts": len(posts),
        "dry_run_ok": checked["ok"],
        "created_ok": created["ok"],
        "resend_replayed": sum(bool(r.get("replayed")) for r in again["results"]),
        "http_calls": 6,
        "llm_tokens": 0,
        "seconds": round(time.monotonic() - started, 2),
        "problems": [r for r in checked["results"] if not r["ok"]],
    }


# --- the AI path: headless Claude Code over MCP --------------------------------------------------


def tasks(stack: Stack) -> list[tuple[str, str, Any]]:
    long_text = (
        "Our kacchi is cooked the old Dhaka way: marinated mutton and rice sealed in one pot and "
        "slow-cooked on dum. "
    ) * 6

    def answer_json(text: str) -> Any:
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start : end + 1]) if start >= 0 else {}

    def pipeline(text: str) -> bool:
        with stack.http(stack.read_key) as c:  # the truth at the moment of asking
            truth = c.get("/v1/overview").json()
        a = answer_json(text)
        return a.get("pipeline_total") == truth["pipeline"]["total"] and str(
            a.get("plan_ends", "")
        ).startswith(str(truth["plan_ends"])[:10])

    def links(text: str) -> bool:
        return answer_json(text).get("links") == [stack.seed["published_url"]]

    def reel_rules(text: str) -> bool:
        a = answer_json(text)
        return (
            a.get("max_files") == 1
            and a.get("caption_max") == 2200
            and ".mp4" in a.get("types", [])
        )

    def drafted(text: str) -> bool:
        p = stack.post("eval-fb-1")
        return bool(p) and p["summary"] == "awaiting_approval"

    def shortened(text: str) -> bool:
        p = stack.post("eval-th-1")
        return bool(p) and p["summary"] == "awaiting_approval"

    def cancelled(text: str) -> bool:
        p = stack.post("eval-fb-1")
        return bool(p) and p["summary"] == "cancelled"

    return [
        (
            "status",
            "How many posts are in the pipeline (waiting for approval or scheduled) and on "
            "which date does the plan end? Answer as JSON "
            '{"pipeline_total": n, "plan_ends": "YYYY-MM-DD"}.',
            pipeline,
        ),
        (
            "history",
            "List the live links of everything published on instagram in the last 7 days. "
            'Answer as JSON {"links": [...]}.',
            links,
        ),
        (
            "rules",
            "What file types, how many files and how many caption characters does an instagram "
            'reel take? Answer as JSON {"types": [...], "max_files": n, "caption_max": n}.',
            reel_rules,
        ),
        (
            "submit",
            f"Hand over this facebook text post as a draft, post_key eval-fb-1, at {slot(3, 18)}: "
            "'Kacchi tonight from 18:00. Order on our site.'",
            drafted,
        ),
        (
            "fix",
            f"Hand over this threads text post as a draft, post_key eval-th-1, at {slot(3, 19)}. "
            f"If it breaks a rule, shorten it but keep the meaning: '{long_text}'",
            shortened,
        ),
        ("cancel", "Cancel the post eval-fb-1.", cancelled),
    ]


def run_agent(stack: Stack, task: str, model: str, work: Path) -> dict[str, Any]:
    cfg = work / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "dk_publish": {
                        "type": "http",
                        "url": f"{stack.base}/mcp/",
                        "headers": {"Authorization": f"Bearer {stack.draft_key}"},
                    }
                }
            }
        )
    )
    cfg.chmod(0o600)
    tools = ",".join(
        f"mcp__dk_publish__{t}"
        for t in (
            "publishing_overview",
            "list_posts",
            "get_post",
            "publishing_spec",
            "submit_posts",
            "cancel_post",
        )
    )
    cmd = [
        "claude",
        "-p",
        task,
        "--output-format",
        "stream-json",
        "--verbose",
        "--mcp-config",
        str(cfg),
        "--strict-mcp-config",
        "--allowedTools",
        tools,
        "--tools",
        "",
        "--system-prompt",
        SYSTEM,
        "--no-session-persistence",
        "--model",
        model,
    ]
    started = time.monotonic()
    env = {**os.environ, "ENABLE_TOOL_SEARCH": "false"}
    out = subprocess.run(
        cmd, cwd=work, env=env, capture_output=True, text=True, timeout=600, check=False
    )
    calls, final = [], {}
    for line in out.stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "assistant":
            calls += [
                c["name"].split("__")[-1]
                for c in e["message"].get("content", [])
                if c.get("type") == "tool_use"
            ]
        elif e.get("type") == "result":
            final = e
    usage = final.get("usage") or {}
    tokens = sum(
        int(usage.get(k) or 0)
        for k in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    )
    return {
        "answer": str(final.get("result") or ""),
        "calls": calls,
        "tokens": tokens,
        "seconds": round(time.monotonic() - started, 1),
        "error": out.stderr[-300:] if out.returncode else "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--database-url", required=True, help="an admin connection; a throwaway database is made"
    )
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--skip-ai", action="store_true")
    ap.add_argument("--out", type=Path, help="write the results as JSON here")
    a = ap.parse_args()
    stack = Stack(a.database_url)
    results: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": a.model,
    }
    try:
        results["code_path"] = code_path(stack)
        print(
            "code path:",
            json.dumps({k: v for k, v in results["code_path"].items() if k != "problems"}),
        )
        if not a.skip_ai:
            results["ai_path"] = []
            work = Path(tempfile.mkdtemp(prefix="dkpub-agent-"))
            try:
                for name, task, check in tasks(stack):
                    r = run_agent(stack, task, a.model, work)
                    r.update(task=name, ok=bool(check(r["answer"])))
                    results["ai_path"].append(r)
                    print(
                        f"{name:8} ok={r['ok']!s:5} calls={','.join(r['calls']) or '-':45} "
                        f"tokens={r['tokens']:>7,} {r['seconds']:>5}s {r['error']}"
                    )
            finally:
                shutil.rmtree(work, ignore_errors=True)
    finally:
        stack.close()
    if a.out:
        a.out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
