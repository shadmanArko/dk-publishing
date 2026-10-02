"""A stateful stand-in for the Threads API, as an httpx transport."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from tests.support import T0
from tests.support.fake_graph import graph_error

THREADS_TOKEN = "THAA-threads-user-token"


class FakeThreads:
    def __init__(self, user: str = "TH1", created: datetime | None = None) -> None:
        self.user = user
        self.created = created or T0 + timedelta(minutes=1)
        self.containers: dict[str, dict[str, Any]] = {}
        self.threads: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.forms: list[dict[str, str]] = []
        self.polls_until_ready = 0  # media containers report IN_PROGRESS this many times first
        self.next_error: httpx.Response | BaseException | None = None
        self.lose_next_response = False
        self.refreshed_token = "THAA-refreshed-token"
        self._n = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def fail_next(self, outcome: httpx.Response | BaseException) -> None:
        self.next_error = outcome

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.next_error is not None:
            outcome, self.next_error = self.next_error, None
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        path = urlparse(str(request.url)).path.split("/", 2)[2]  # drop /v1.0/
        if request.method == "GET":
            query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
            return self._get(path, query)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        if form.get("access_token") != THREADS_TOKEN:
            return graph_error(400, 190, "Invalid OAuth access token.", 467)
        response = self._post(path, form)
        if self.lose_next_response:
            self.lose_next_response = False
            raise httpx.ReadTimeout("the response was lost")
        return response

    def _post(self, path: str, form: dict[str, str]) -> httpx.Response:
        user, edge = path.split("/", 1)
        if user != self.user:
            return graph_error(400, 100, "Unsupported post request. Object does not exist.")
        self._n += 1
        if edge == "threads":
            kind = form.get("media_type", "")
            if kind == "TEXT" and not form.get("text"):
                return graph_error(400, 100, "(#100) text is required for a TEXT post")
            cid = f"C{self._n}"
            self.containers[cid] = {"form": form, "polls": 0, "published": False}
            return httpx.Response(200, json={"id": cid})
        if edge == "threads_publish":
            container = self.containers.get(form.get("creation_id", ""))
            if container is None:
                return graph_error(400, 24, "The requested resource does not exist")
            if container["published"]:
                return graph_error(400, 100, "This container was already published")
            container["published"] = True
            tid = f"T{self._n}"
            self.threads.append(
                {"id": tid, "text": container["form"].get("text", ""), "permalink": f"https://www.threads.net/@dk/post/{tid}",
                 "timestamp": self.created.strftime("%Y-%m-%dT%H:%M:%S+0000"), "container": form["creation_id"]}
            )  # fmt: skip
            return httpx.Response(200, json={"id": tid})
        return graph_error(400, 100, f"Unknown edge {edge}")

    def _get(self, path: str, query: dict[str, str]) -> httpx.Response:
        if path == "refresh_access_token":
            if query.get("access_token") not in (THREADS_TOKEN, self.refreshed_token):
                return graph_error(400, 190, "Invalid OAuth access token.", 467)
            return httpx.Response(
                200,
                json={
                    "access_token": self.refreshed_token,
                    "token_type": "bearer",
                    "expires_in": 5184000,
                },
            )
        if query.get("access_token") != THREADS_TOKEN:
            return graph_error(400, 190, "Invalid OAuth access token.", 467)
        if path == "me":
            return httpx.Response(200, json={"id": self.user, "username": "dhakakacchi"})
        if path == f"{self.user}/threads":
            return httpx.Response(200, json={"data": [t for t in reversed(self.threads)]})
        if path in self.containers:
            container = self.containers[path]
            if container["published"]:
                status = "PUBLISHED"
            elif (
                container["form"].get("media_type") == "TEXT"
                or container["polls"] >= self.polls_until_ready
            ):
                status = "FINISHED"
            else:
                container["polls"] += 1
                status = "IN_PROGRESS"
            return httpx.Response(200, json={"id": path, "status": status})
        for t in self.threads:
            if t["id"] == path:
                return httpx.Response(200, json={"id": path, "permalink": t["permalink"]})
        return graph_error(400, 100, "Unsupported get request.")

    def posted(self) -> list[dict[str, Any]]:
        return self.threads
