"""Webhooks against a real database: registering, which events reach whom, retries, giving up."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from dagster import DagsterInstance, RunRequest, SkipReason, build_sensor_context
from fastapi.testclient import TestClient

from dk_publishing.adapters.notify.webhooks import HttpWebhookSender
from dk_publishing.application.use_cases import api_keys
from dk_publishing.application.use_cases.webhooks import (
    MAX_ATTEMPTS,
    RETRY_GAPS,
    deliver_due,
    enqueue_events,
    has_work,
)
from dk_publishing.domain.webhooks import verify
from dk_publishing.entrypoints.dagster_defs.jobs import send_webhooks
from dk_publishing.entrypoints.dagster_defs.resources import WebhookResource
from dk_publishing.entrypoints.dagster_defs.sensors import webhook_delivery
from dk_publishing.entrypoints.publish_api.app import create_app
from tests.integration.test_api import Rig

URL = "https://agent.example.com/hooks"


class Receiver:
    """The client's server: records every request and answers as scripted."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: list[int] = []  # popped first-to-last; empty means 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.answers.pop(0) if self.answers else 200)

    @property
    def types(self) -> list[str]:
        return [r.headers["x-dk-event"] for r in self.requests]

    def body(self, index: int = -1) -> dict[str, Any]:
        return dict(json.loads(self.requests[index].content))


class Hooks:
    def __init__(self, conninfo: str, tmp_path: Path) -> None:
        self.rig = Rig(conninfo, tmp_path)
        self.receiver = Receiver()
        self.sender = HttpWebhookSender(
            transport=httpx.MockTransport(self.receiver), resolver=lambda _: ["93.184.216.34"]
        )
        self.rig.api = dataclasses.replace(self.rig.api, webhooks=self.sender)
        self.rig.client = TestClient(create_app(self.rig.api))
        self.core = self.rig.api.core

    def register(self, scope: str = "draft", url: str = URL, **body: Any) -> Any:
        return self.rig.call("POST", "/v1/webhooks", scope, json={"url": url, **body})

    def secret(self, **body: Any) -> str:
        response = self.register(**body)
        assert response.status_code == 201, response.text
        return str(response.json()["secret"])

    def run(self) -> Any:
        return deliver_due(self.core, self.sender, self.rig.notifier)

    def approve(self, key: str = "campaign-1") -> None:
        assert self.rig.post(self.rig.reel(key)).status_code == 201
        assert self.rig.call("POST", f"/v1/posts/{key}/approve", "approve").status_code == 200

    def messages(self) -> list[dict[str, Any]]:
        [hook] = self.rig.call("GET", "/v1/webhooks", "draft").json()["webhooks"]
        return list(
            self.rig.call("GET", f"/v1/webhooks/{hook['id']}/deliveries", "draft").json()[
                "deliveries"
            ]
        )


@pytest.fixture
def hooks(conninfo: str, tmp_path: Path) -> Hooks:
    return Hooks(conninfo, tmp_path)


# --- registering ------------------------------------------------------------------------------


def test_the_secret_is_shown_once_and_never_listed(hooks: Hooks) -> None:
    created = hooks.register().json()
    assert created["secret"].startswith("whsec_") and created["events"] == "all"
    listed = hooks.rig.call("GET", "/v1/webhooks", "draft").json()["webhooks"]
    assert [h["id"] for h in listed] == [created["id"]]
    assert "secret" not in json.dumps(listed)


def test_a_bare_curl_without_a_json_label_can_register_a_webhook(hooks: Hooks) -> None:
    """`curl -d '{...}'` labels the body as a form: the owner hit this on the first live try."""
    headers = {"Authorization": f"Bearer {hooks.rig.keys['draft']}"}
    for label in (None, "application/x-www-form-urlencoded"):
        if label:
            headers["Content-Type"] = label
        response = hooks.rig.client.post(
            "/v1/webhooks", content=json.dumps({"url": URL}), headers=headers
        )
        assert response.status_code == 201, (label, response.text)
    assert len(hooks.rig.call("GET", "/v1/webhooks", "draft").json()["webhooks"]) == 2


@pytest.mark.parametrize(
    ("url", "code"),
    [("http://agent.example.com/h", "bad_url"), ("https://u:p@agent.example.com", "bad_url")],
)
def test_bad_addresses_are_refused_with_a_reason(hooks: Hooks, url: str, code: str) -> None:
    response = hooks.register(url=url)
    assert response.status_code == 422
    assert response.json()["problems"][0] == {
        "path": "url", "code": code, "message": response.json()["problems"][0]["message"],
    }  # fmt: skip


def test_private_hosts_unknown_events_and_long_addresses_are_refused(hooks: Hooks) -> None:
    private = HttpWebhookSender(resolver=lambda _: ["10.0.0.7"])
    hooks.rig.api = dataclasses.replace(hooks.rig.api, webhooks=private)
    hooks.rig.client = TestClient(create_app(hooks.rig.api))
    assert hooks.register().json()["problems"][0]["code"] == "bad_url"
    hooks.rig.api = dataclasses.replace(hooks.rig.api, webhooks=hooks.sender)
    hooks.rig.client = TestClient(create_app(hooks.rig.api))
    wrong = hooks.register(events=["post.exploded"]).json()["problems"][0]
    assert wrong["code"] == "not_allowed" and "post.published" in wrong["allowed"]
    assert hooks.register(url="https://a.example.com/" + "x" * 2000).status_code == 422


def test_a_key_may_have_three_webhooks_and_a_read_key_none(hooks: Hooks) -> None:
    for _ in range(3):
        assert hooks.register().status_code == 201
    assert hooks.register().status_code == 409
    assert hooks.register("read").status_code == 403


def test_a_key_sees_and_deletes_only_its_own(hooks: Hooks) -> None:
    mine = hooks.register().json()["id"]
    assert hooks.rig.call("GET", "/v1/webhooks", "approve").json() == {"webhooks": []}
    assert hooks.rig.call("DELETE", f"/v1/webhooks/{mine}", "approve").status_code == 404
    assert hooks.rig.call("GET", f"/v1/webhooks/{mine}/deliveries", "approve").status_code == 404
    assert hooks.rig.call("DELETE", f"/v1/webhooks/{mine}", "draft").json() == {"deleted": True}
    assert hooks.rig.call("DELETE", f"/v1/webhooks/{mine}", "draft").status_code == 404
    assert hooks.rig.call("DELETE", "/v1/webhooks/not-a-uuid", "draft").status_code == 404


def test_ping_sends_a_signed_test_message_and_says_what_the_receiver_answered(
    hooks: Hooks,
) -> None:
    secret = hooks.secret()
    [hook] = hooks.rig.call("GET", "/v1/webhooks", "draft").json()["webhooks"]
    assert hooks.rig.call("POST", f"/v1/webhooks/{hook['id']}/ping", "draft").json() == {
        "delivered": True, "http_status": 200,
    }  # fmt: skip
    request = hooks.receiver.requests[-1]
    assert request.headers["x-dk-event"] == "ping"
    assert verify(secret, request.headers["x-dk-signature"], request.content, _now(hooks))
    hooks.receiver.answers.append(500)
    failed = hooks.rig.call("POST", f"/v1/webhooks/{hook['id']}/ping", "draft").json()
    assert failed == {"delivered": False, "http_status": 500, "error": "HTTP 500"}
    assert hooks.rig.call("POST", f"/v1/webhooks/{hook['id']}/ping", "approve").status_code == 404


def _now(hooks: Hooks) -> int:
    return int(hooks.rig.clock.now().timestamp())


# --- which events reach whom ------------------------------------------------------------------


def test_an_approval_is_delivered_once_signed_and_with_the_posts_details(hooks: Hooks) -> None:
    secret = hooks.secret()
    hooks.approve()
    report = hooks.run()
    assert (report.queued, report.delivered) == (1, 1) and hooks.receiver.types == ["post.approved"]
    request, body = hooks.receiver.requests[0], hooks.receiver.body()
    assert verify(secret, request.headers["x-dk-signature"], request.content, _now(hooks))
    assert body["type"] == "post.approved" and body["id"] == request.headers["x-dk-event-id"]
    assert body["data"] | {"variant_id": None, "slot": None} == {
        "post_key": "campaign-1", "variant_id": None, "platform": "instagram",
        "account": "Dhaka Kacchi", "format": "reel", "state": "scheduled", "status": "approved",
        "slot": None, "live_url": None, "handed_over": False, "reason": "approved",
    }  # fmt: skip
    again = hooks.run()  # nothing new, nothing sent twice
    assert (again.queued, again.delivered) == (0, 0) and len(hooks.receiver.requests) == 1


def test_a_cancellation_says_who_cancelled_and_why(hooks: Hooks) -> None:
    hooks.secret()
    assert hooks.rig.post(hooks.rig.reel("c")).status_code == 201
    assert hooks.rig.call("DELETE", "/v1/posts/c").status_code == 200
    hooks.run()
    assert hooks.receiver.types == ["post.cancelled"]
    assert hooks.receiver.body()["data"]["reason"] == "cancelled through the API"


def test_a_draft_alone_sends_nothing(hooks: Hooks) -> None:
    hooks.secret()
    assert hooks.rig.post(hooks.rig.reel("c")).status_code == 201
    assert hooks.run().queued == 0 and hooks.receiver.requests == []


def test_an_endpoint_that_asked_for_some_events_hears_only_those(hooks: Hooks) -> None:
    hooks.secret(events=["post.published"])
    hooks.approve()
    assert hooks.run().queued == 0 and hooks.receiver.requests == []


def test_history_from_before_the_endpoint_existed_is_not_replayed(
    conninfo: str, tmp_path: Path
) -> None:
    hooks = Hooks(conninfo, tmp_path)
    hooks.rig.clock.set(datetime(2026, 1, 5, tzinfo=UTC))  # long before the endpoint's own clock
    hooks.approve("old")
    hooks.secret()
    assert hooks.run().queued == 0 and hooks.receiver.requests == []


def test_another_keys_posts_are_never_sent_to_this_endpoint(hooks: Hooks) -> None:
    hooks.secret()
    other = api_keys.create_key(hooks.core, "dk", "other-agent", "approve")
    hooks.rig.keys["other"] = other
    other_post = hooks.rig.reel("theirs")
    assert hooks.rig.post(other_post, scope="other").status_code == 201
    assert hooks.rig.call("POST", "/v1/posts/theirs/approve", "other").status_code == 200
    assert hooks.run().queued == 0 and hooks.receiver.requests == []


def test_a_revoked_key_stops_receiving(hooks: Hooks) -> None:
    hooks.secret()
    hooks.approve()
    api_keys.revoke_key(hooks.core, "dk", "draft-key")
    assert hooks.run().queued == 0 and hooks.receiver.requests == []


# --- retries and giving up --------------------------------------------------------------------


def test_a_failing_receiver_is_retried_with_growing_gaps_and_the_same_message_id(
    hooks: Hooks,
) -> None:
    hooks.secret()
    hooks.approve()
    hooks.receiver.answers += [500, 503]
    assert hooks.run().retrying == 1
    [first] = hooks.messages()
    assert first["status"] == "pending" and first["attempts"] == 1 and first["http_status"] == 500
    assert hooks.run().delivered == 0 and len(hooks.receiver.requests) == 1  # not due yet
    hooks.rig.clock.advance(RETRY_GAPS[0])
    assert hooks.run().retrying == 1  # 503
    hooks.rig.clock.advance(RETRY_GAPS[1])
    assert hooks.run().delivered == 1  # 200 at last
    ids = {r.headers["x-dk-event-id"] for r in hooks.receiver.requests}
    assert len(hooks.receiver.requests) == 3 and len(ids) == 1
    assert hooks.messages()[0]["status"] == "delivered" and hooks.messages()[0]["attempts"] == 3


def test_a_message_is_given_up_after_the_last_attempt_and_the_owner_is_told_once(
    hooks: Hooks,
) -> None:
    hooks.secret()
    hooks.approve()
    hooks.receiver.answers += [500] * 20
    for attempt in range(MAX_ATTEMPTS):
        report = hooks.run()
        assert (report.dead, report.retrying) == ((1, 0) if attempt == MAX_ATTEMPTS - 1 else (0, 1))
        hooks.rig.clock.advance(timedelta(hours=25))
    [message] = hooks.messages()
    assert message["status"] == "dead" and message["attempts"] == MAX_ATTEMPTS
    assert len(hooks.receiver.requests) == MAX_ATTEMPTS
    hooks.run()  # a dead message is never tried again
    assert len(hooks.receiver.requests) == MAX_ATTEMPTS
    [alert] = [m for m in hooks.rig.notifier.sent if "webhook" in m]  # the draft notice is another
    assert "could not be delivered" in alert and "agent.example.com" in alert
    assert "hooks?" not in alert and "/hooks" not in alert  # only the host, never the path


def test_a_410_switches_the_endpoint_off(hooks: Hooks) -> None:
    hooks.secret()
    hooks.approve()
    hooks.receiver.answers.append(410)
    assert hooks.run().dead == 1
    [hook] = hooks.rig.call("GET", "/v1/webhooks", "draft").json()["webhooks"]
    assert hook["active"] is False and "410" in hook["disabled_reason"]
    hooks.approve("later")
    assert hooks.run().queued == 0
    assert hooks.rig.call("POST", f"/v1/webhooks/{hook['id']}/ping", "draft").status_code == 404


def test_two_workers_never_hold_the_same_message(hooks: Hooks) -> None:
    hooks.secret()
    hooks.approve()
    enqueue_events(hooks.core)
    now = hooks.rig.clock.now()
    with hooks.core.uow() as uow:
        first = uow.webhooks.claim_due(now, 10, timedelta(minutes=5))
        uow.commit()
    with hooks.core.uow() as uow:
        second = uow.webhooks.claim_due(now, 10, timedelta(minutes=5))
        later = uow.webhooks.claim_due(now + timedelta(minutes=6), 10, timedelta(minutes=5))
    assert len(first) == 1 and second == [] and len(later) == 1  # a crashed worker's lease expires


def test_deleting_a_webhook_removes_its_waiting_messages(hooks: Hooks) -> None:
    hooks.secret()
    hooks.approve()
    enqueue_events(hooks.core)
    [hook] = hooks.rig.call("GET", "/v1/webhooks", "draft").json()["webhooks"]
    hooks.rig.call("DELETE", f"/v1/webhooks/{hook['id']}", "draft")
    assert hooks.run().delivered == 0 and hooks.receiver.requests == []


# --- the Dagster shell ------------------------------------------------------------------------

_HOOKS: dict[str, Hooks] = {}


class FakeWebhookResource(WebhookResource):
    """The real resource, but on the test's clock, receiver and notifier."""

    def has_work(self) -> bool:
        return has_work(_HOOKS[self.database_url].core)

    def deliver(self) -> Any:
        hooks = _HOOKS[self.database_url]
        return deliver_due(hooks.core, hooks.sender, hooks.rig.notifier)


def test_the_sensor_starts_a_run_only_when_there_is_something_to_send(
    hooks: Hooks, conninfo: str
) -> None:
    _HOOKS[conninfo] = hooks
    resource = FakeWebhookResource(database_url=conninfo)
    sensor = webhook_delivery

    def tick(cursor: str | None = None) -> list[Any]:
        context = build_sensor_context(resources={"hooks": resource}, cursor=cursor)
        return list(sensor(context))  # type: ignore[arg-type]

    [idle] = tick()
    assert isinstance(idle, SkipReason)
    hooks.secret()
    hooks.approve()  # an unreported state change counts as work before it is queued
    [request] = tick()
    assert isinstance(request, RunRequest) and request.tags == {"dk/webhooks": "send"}
    result = send_webhooks.execute_in_process(
        resources={"hooks": resource}, instance=DagsterInstance.ephemeral()
    )
    assert result.success and hooks.receiver.types == ["post.approved"]
    assert isinstance(tick()[0], SkipReason)  # delivered: quiet again


# --- the owner's commands ---------------------------------------------------------------------


def test_the_owner_can_list_and_switch_off_webhooks_and_the_address_path_stays_hidden(
    hooks: Hooks,
    conninfo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dk_publishing.entrypoints.cli import main

    hooks.secret(url="https://agent.example.com/secret-path?token=abc")
    config = tmp_path / "dk.json"
    config.write_text("{}")
    monkeypatch.setenv("DATABASE_URL", conninfo)
    monkeypatch.setenv("DK_CONFIG_FILE", str(config))
    assert main(["api", "webhooks"]) == 0
    listing = capsys.readouterr().out
    assert "draft-key" in listing and "agent.example.com" in listing and "active" in listing
    assert "secret-path" not in listing and "token=abc" not in listing and "whsec_" not in listing
    [hook] = hooks.rig.call("GET", "/v1/webhooks", "draft").json()["webhooks"]
    assert main(["api", "webhook-disable", hook["id"]]) == 0
    assert main(["api", "webhook-disable", hook["id"]]) == 1  # already off
    assert "OFF" in (main(["api", "webhooks"]), capsys.readouterr().out)[1]
