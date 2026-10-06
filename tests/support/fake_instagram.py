"""A stateful stand-in for the Instagram Graph API (Facebook Login), including the upload host."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from tests.support import T0
from tests.support.fake_graph import TOKEN, graph_error


class FakeInstagram:
    def __init__(self, account: str = "IG1", created: datetime | None = None) -> None:
        self.account = account
        self.created = created or T0 + timedelta(minutes=1)
        self.containers: dict[str, dict[str, Any]] = {}
        self.media: list[dict[str, Any]] = []
        self.stories: list[dict[str, Any]] = []  # stories are not part of the media list
        self.uploads: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.forms: list[dict[str, str]] = []
        self.polls_until_ready = 0
        self.quota = (0, 100)
        self.quota_readable = True
        self.next_error: httpx.Response | BaseException | None = None
        self.only: str | None = None  # restrict the next failure to GET or POST
        self._n = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def fail_next(self, outcome: httpx.Response | BaseException, only: str | None = None) -> None:
        self.next_error, self.only = outcome, only

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.next_error is not None and self.only in (None, request.method):
            outcome, self.next_error = self.next_error, None
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        if request.url.host == "rupload.facebook.com":
            return self._upload(request)
        path = urlparse(str(request.url)).path.split("/", 2)[2]  # drop /v25.0/
        if request.method == "GET":
            query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
            if query.get("access_token") != TOKEN:
                return graph_error(400, 190, "Invalid OAuth access token.", 467)
            return self._get(path)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        if form.get("access_token") != TOKEN:
            return graph_error(400, 190, "Invalid OAuth access token.", 467)
        return self._post(path, form)

    def _upload(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"OAuth {TOKEN}":
            return graph_error(400, 190, "Invalid OAuth access token.", 467)
        container = request.url.path.rstrip("/").split("/")[-1]
        body = request.content
        if (
            request.headers.get("file_size") != str(len(body))
            or request.headers.get("offset") != "0"
        ):
            return httpx.Response(400, json={"debug_info": {"message": "bad upload headers"}})
        self.uploads.append(
            {"container": container, "bytes": body, "headers": dict(request.headers)}
        )
        if container in self.containers:
            self.containers[container]["uploaded"] = True
        return httpx.Response(200, json={"success": True, "message": "Upload successful"})

    def _post(self, path: str, form: dict[str, str]) -> httpx.Response:
        account, edge = path.split("/", 1)
        if account != self.account:
            return graph_error(400, 100, "Unsupported post request. Object does not exist.")
        self._n += 1
        if edge == "media":
            if form.get("media_type") not in (None, "REELS", "IMAGE", "CAROUSEL", "STORIES"):
                return graph_error(400, 100, "Invalid media_type")
            cid = f"C{self._n}"
            if form.get("media_type") == "CAROUSEL":
                kids = [k for k in form.get("children", "").split(",") if k]
                if not 2 <= len(kids) <= 10 or any(k not in self.containers for k in kids):
                    return graph_error(400, 100, "Invalid children for the carousel")
            resumable = form.get("upload_type") == "resumable"
            if form.get("video_url") and not form["video_url"].startswith("https://"):
                return graph_error(400, 9004, "The media could not be fetched from that URL")
            self.containers[cid] = {
                "form": form,
                "polls": 0,
                "uploaded": not resumable,
                "published": False,
            }
            body: dict[str, Any] = {"id": cid}
            if resumable:
                body["uri"] = f"https://rupload.facebook.com/ig-api-upload/v25.0/{cid}"
            return httpx.Response(200, json=body)
        if edge == "media_publish":
            container = self.containers.get(form.get("creation_id", ""))
            if container is None or container["published"]:
                return graph_error(400, 100, "Invalid or already published container")
            container["published"] = True
            mid = f"M{self._n}"
            if container["form"].get("media_type") == "STORIES":
                self.stories.append({"id": mid, "form": container["form"]})
                return httpx.Response(200, json={"id": mid})  # stories are not in the media list
            self.media.append(
                {"id": mid, "caption": container["form"].get("caption", ""),
                 "permalink": f"https://www.instagram.com/reel/{mid}/",
                 "timestamp": self.created.strftime("%Y-%m-%dT%H:%M:%S+0000")}
            )  # fmt: skip
            return httpx.Response(200, json={"id": mid})
        return graph_error(400, 100, f"Unknown edge {edge}")

    def _get(self, path: str) -> httpx.Response:
        if path == self.account:
            return httpx.Response(200, json={"id": self.account, "username": "dhakakacchi"})
        if path == f"{self.account}/media":
            return httpx.Response(200, json={"data": list(reversed(self.media))})
        if path == f"{self.account}/content_publishing_limit":
            if not self.quota_readable:
                return graph_error(400, 100, "(#100) cannot read the limit")
            used, total = self.quota
            return httpx.Response(
                200, json={"data": [{"quota_usage": used, "config": {"quota_total": total}}]}
            )
        if path in self.containers:
            container = self.containers[path]
            if container["published"]:
                status = "PUBLISHED"
            elif container["uploaded"] and container["polls"] >= self.polls_until_ready:
                status = "FINISHED"
            else:
                container["polls"] += 1
                status = "IN_PROGRESS"
            return httpx.Response(200, json={"id": path, "status_code": status})
        for item in self.media:
            if item["id"] == path:
                return httpx.Response(200, json={"id": path, "permalink": item["permalink"]})
        return graph_error(400, 100, "Unsupported get request.")
