"""A stateful stand-in for the Meta Graph API, as an httpx transport.

It keeps the Page's posts, videos and photos, so publishing something makes it show up when the
feed is read back, exactly what `find_live` relies on.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from tests.support import T0

TOKEN = "EAAB-secret-page-token"


def graph_error(status: int, code: int, message: str, subcode: int | None = None) -> httpx.Response:
    error: dict[str, Any] = {"message": message, "type": "OAuthException", "code": code}
    if subcode:
        error["error_subcode"] = subcode
    return httpx.Response(status, json={"error": error})


def parse_multipart(request: httpx.Request) -> dict[str, Any]:
    """Field values (and the raw bytes of file parts) from a multipart body."""
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    out: dict[str, Any] = {}
    for part in request.content.split(b"--" + boundary):
        match = re.search(
            rb'name="([^"]+)"(?:; filename="[^"]*")?\r\n(?:[^\r\n]+\r\n)*\r\n(.*)\r\n$', part, re.S
        )
        if match:
            out[match.group(1).decode()] = match.group(2)
    return out


class FakeGraph:
    def __init__(self, page: str = "PAGE", created: datetime | None = None) -> None:
        self.page = page
        self.created = created or T0 + timedelta(minutes=1)
        self.items: dict[str, list[dict[str, Any]]] = {"feed": [], "videos": [], "photos": []}
        self.requests: list[httpx.Request] = []
        self.forms: list[dict[str, Any]] = []
        self.hosts: list[str] = []
        self.next_error: Callable[[httpx.Request], httpx.Response | BaseException] | None = None
        self.lose_next_response = False
        self.name = "Dhaka Kacchi"
        self.app_token = "936388502452682|APPSECRET"
        self.debug: dict[str, Any] = {
            "type": "PAGE", "is_valid": True, "expires_at": 0,
            "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
        }  # fmt: skip
        self._n = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- the API ---
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.hosts.append(request.url.host)
        if self.next_error is not None:
            fail, self.next_error = self.next_error, None
            outcome = fail(request)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        path = urlparse(str(request.url)).path.split("/", 2)[2]  # drop /v25.0/
        if request.method == "DELETE":
            return self._delete(path, urlparse(str(request.url)).query)
        if request.method == "GET":
            query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
            if path == "debug_token":
                if query.get("access_token") != self.app_token:
                    return graph_error(400, 190, "Invalid app credentials.")
                return httpx.Response(200, json={"data": self.debug})
            if query.get("access_token") != TOKEN:
                return graph_error(400, 190, "Invalid OAuth access token.", 467)
            return self._get(path, query)
        form = self._form(request)
        self.forms.append(form)
        if form.get("access_token") != TOKEN:
            return graph_error(400, 190, "Invalid OAuth access token.", 467)
        response = self._post(path, form)
        if self.lose_next_response:
            # Meta did the work, but the answer never reached us: the dangerous case.
            self.lose_next_response = False
            raise httpx.ReadTimeout("the response was lost")
        return response

    @staticmethod
    def _form(request: httpx.Request) -> dict[str, Any]:
        if request.headers.get("content-type", "").startswith("multipart/"):
            raw = parse_multipart(request)
            return {k: (v if k == "source" else v.decode()) for k, v in raw.items()}
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    def _delete(self, path: str, query: str) -> httpx.Response:
        if parse_qs(query).get("access_token", [""])[0] != TOKEN:
            return graph_error(400, 190, "Invalid OAuth access token.", 467)
        for items in self.items.values():
            for item in items:
                if item["id"] == path:
                    items.remove(item)
                    return httpx.Response(200, json={"success": True})
        return graph_error(400, 100, "Unsupported delete request. Object does not exist.")

    def _post(self, path: str, form: dict[str, Any]) -> httpx.Response:
        page, edge = path.split("/", 1)
        if page != self.page:
            return graph_error(400, 100, "Unsupported post request. Object does not exist.")
        self._n += 1
        n, created = self._n, self.created.strftime("%Y-%m-%dT%H:%M:%S+0000")
        held = form.get("published") == "false"
        due = (
            datetime.fromtimestamp(int(form["scheduled_publish_time"]), tz=self.created.tzinfo)
            if held and form.get("scheduled_publish_time")
            else None
        )
        if held and due is None:
            return graph_error(400, 100, "(#100) scheduled_publish_time is required")
        if edge == "feed":
            if not form.get("message") and not form.get("link"):
                return graph_error(400, 100, "(#100) The parameter message is required")
            post_id = f"{page}_{n}"
            self.items["feed"].append(
                {"id": post_id, "message": form.get("message", ""), "created_time": created,
                 "permalink_url": f"/{page}/posts/{n}", "is_published": not held, "due": due}
            )  # fmt: skip
            return httpx.Response(200, json={"id": post_id})
        if edge == "photos":
            post_id = f"{page}_{n}"
            self.items["photos"].append(
                {"id": f"photo{n}", "name": form.get("caption", ""), "created_time": created,
                 "permalink_url": f"/photo.php?fbid={n}"}
            )  # fmt: skip
            return httpx.Response(200, json={"id": f"photo{n}", "post_id": post_id})
        if edge == "videos":
            self.items["videos"].append(
                {"id": f"vid{n}", "description": form.get("description", ""), "created_time": created,
                 "permalink_url": f"/{page}/videos/{n}", "published": not held, "due": due}
            )  # fmt: skip
            return httpx.Response(200, json={"id": f"vid{n}"})
        return graph_error(400, 100, f"Unknown edge {edge}")

    def _get(self, path: str, query: dict[str, str]) -> httpx.Response:
        if "/" in path:
            _, edge = path.split("/", 1)
            visible = [
                i
                for i in self.items.get(edge, [])
                if i.get("is_published", i.get("published", True))
            ]  # a post still being held is not on the feed
            return httpx.Response(200, json={"data": [_public(i) for i in reversed(visible)]})
        if path == self.page:
            return httpx.Response(200, json={"id": self.page, "name": self.name})
        for items in self.items.values():
            for item in items:
                if item["id"] == path:
                    flags = {k: item[k] for k in ("is_published", "published") if k in item}
                    return httpx.Response(
                        200, json={"permalink_url": item["permalink_url"], "id": path, **flags}
                    )
        return graph_error(400, 100, "Unsupported get request.")

    # --- helpers for tests ---
    def posted(self, edge: str = "feed") -> list[dict[str, Any]]:
        """Everything on the edge, published or still being held."""
        return self.items[edge]

    def held(self, edge: str = "feed") -> list[dict[str, Any]]:
        return [i for i in self.items[edge] if not i.get("is_published", i.get("published", True))]

    def publish_due(self, now: datetime) -> None:
        """Facebook publishing what it was holding, as time passes."""
        for items in self.items.values():
            for item in items:
                due = item.get("due")
                if due is not None and due <= now:
                    for key in ("is_published", "published"):
                        if key in item:
                            item[key] = True

    def lose_response_next(self) -> None:
        self.lose_next_response = True

    def fail_next(self, outcome: httpx.Response | BaseException) -> None:
        self.next_error = lambda request: outcome


def body_of(request: httpx.Request) -> str:
    return json.dumps(request.content.decode(errors="replace"))


def _public(item: dict[str, Any]) -> dict[str, Any]:
    """What Facebook would show: the item without this fake's internal bookkeeping."""
    return {k: v for k, v in item.items() if k != "due"}
