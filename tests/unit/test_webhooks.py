"""Signing and sending webhook messages, without a database."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from dk_publishing.adapters.notify.webhooks import HttpWebhookSender
from dk_publishing.application.ports import WebhookCandidate, WebhookUrlRejected
from dk_publishing.application.use_cases.webhooks import event_id, payload_for
from dk_publishing.domain.status import VariantStatus
from dk_publishing.domain.webhooks import EVENT_FOR_STATUS, EVENTS, body_bytes, sign, verify

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
SECRET = "whsec_test"
PUBLIC = "93.184.216.34"


def public(_: str) -> list[str]:
    return [PUBLIC]


def sender(handler: httpx.MockTransport | None = None, resolver=public) -> HttpWebhookSender:  # type: ignore[no-untyped-def]
    return HttpWebhookSender(transport=handler, resolver=resolver)


def send(s: HttpWebhookSender, url: str = "https://agent.example.com/hooks?k=1"):  # type: ignore[no-untyped-def]
    body = body_bytes({"id": "evt_1", "type": "post.published", "data": {"post_key": "p"}})
    return s.send(url, SECRET, delivery_id="d1", event_id="evt_1", event_type="post.published",
                  body=body, now=NOW)  # fmt: skip


# --- the signature ----------------------------------------------------------------------------


def test_a_receiver_can_verify_a_message_and_refuses_a_forged_or_stale_one() -> None:
    body = body_bytes({"a": 1})
    header = sign(SECRET, int(NOW.timestamp()), body)
    now = int(NOW.timestamp())
    assert verify(SECRET, header, body, now)
    assert not verify(SECRET, header, body + b" ", now)  # the body was changed
    assert not verify("whsec_other", header, body, now)  # signed with another secret
    assert not verify(SECRET, header, body, now + 301)  # too old: a replayed message
    assert not verify(SECRET, "garbage", body, now) and not verify(SECRET, "", body, now)


def test_the_body_is_compact_and_in_a_stable_order() -> None:
    assert body_bytes({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


# --- where it is allowed to go ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://agent.example.com/h",
        "https://user:pw@agent.example.com/h",
        "ftp://x.example.com",
        "https:///x",
    ],
)
def test_only_plain_https_addresses_without_credentials_are_accepted(url: str) -> None:
    with pytest.raises(WebhookUrlRejected):
        sender().check(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254", "::1"]
)
def test_private_and_local_hosts_are_refused_even_if_one_answer_is_public(address: str) -> None:
    mixed = sender(resolver=lambda _: [PUBLIC, address])
    with pytest.raises(WebhookUrlRejected, match="public host"):
        mixed.check("https://agent.example.com/h")
    assert not send(mixed).ok  # sending re-checks: a DNS answer that changed since registering


def test_a_host_that_does_not_resolve_is_refused() -> None:
    with pytest.raises(WebhookUrlRejected):
        sender(resolver=lambda _: []).check("https://nowhere.example.com/h")


# --- what is sent -----------------------------------------------------------------------------


def test_the_connection_goes_to_the_checked_address_with_the_clients_name_and_a_valid_signature() -> (
    None
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    result = send(sender(httpx.MockTransport(handler)))
    [request] = seen
    assert result.ok and result.http_status == 204
    assert request.url.host == PUBLIC  # pinned: no second DNS lookup can change the target
    assert request.url.query == b"k=1" and request.url.path == "/hooks"
    assert request.headers["host"] == "agent.example.com"
    assert request.extensions["sni_hostname"] == "agent.example.com"
    assert (request.headers["x-dk-event"], request.headers["x-dk-event-id"]) == (
        "post.published",
        "evt_1",
    )
    assert request.headers["x-dk-delivery"] == "d1"
    assert verify(SECRET, request.headers["x-dk-signature"], request.content, int(NOW.timestamp()))
    assert json.loads(request.content)["data"]["post_key"] == "p"


def test_an_ipv6_address_is_bracketed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    v6 = sender(
        httpx.MockTransport(handler), resolver=lambda _: ["2606:2800:220:1:248:1893:25c8:1946"]
    )
    assert send(v6).ok and seen[0].url.host == "2606:2800:220:1:248:1893:25c8:1946"


# --- what comes back --------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "ok", "gone"), [(200, True, False), (202, True, False),
                                                     (500, False, False), (404, False, False),
                                                     (410, False, True)])  # fmt: skip
def test_the_answer_decides_success_and_a_410_means_gone(status: int, ok: bool, gone: bool) -> None:
    result = send(sender(httpx.MockTransport(lambda r: httpx.Response(status))))
    assert (result.ok, result.gone, result.http_status) == (ok, gone, status)


def test_a_redirect_is_a_failure_and_is_never_followed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    result = send(sender(httpx.MockTransport(handler)))
    assert not result.ok and "redirects are not followed" in (result.error or "")
    assert len(calls) == 1


def test_failures_never_leak_the_address_or_the_body() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom https://agent.example.com/hooks?k=1")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    for handler, text in ((refuse, "could not connect"), (slow, "timed out")):
        result = send(sender(httpx.MockTransport(handler)))
        assert not result.ok and result.error == text and "example.com" not in (result.error or "")


# --- the message itself -----------------------------------------------------------------------


def candidate(status: VariantStatus, **more: object) -> WebhookCandidate:
    base = dict(
        endpoint_id="e", variant_id="v1", seq=3, event_type=EVENT_FOR_STATUS[status],
        at=NOW, post_key="eid-1", platform="instagram", account="Dhaka Kacchi", format="reel",
        status=status.value, slot=NOW + timedelta(days=1), live_url="https://ig.example/p/1",
        reason="because", handed_over=False,
    )  # fmt: skip
    return WebhookCandidate(**{**base, **more})  # type: ignore[arg-type]


def test_a_published_message_carries_the_link_and_other_messages_do_not() -> None:
    live = payload_for(candidate(VariantStatus.PUBLISHED))
    assert live["type"] == "post.published" and live["id"] == "evt_v1_3" == event_id(
        candidate(VariantStatus.PUBLISHED)
    )
    assert (
        live["data"]["live_url"] == "https://ig.example/p/1"
        and live["data"]["state"] == "published"
    )
    for status in (VariantStatus.APPROVED, VariantStatus.FAILED, VariantStatus.UNKNOWN):
        data = payload_for(candidate(status))["data"]
        assert data["live_url"] is None and data["post_key"] == "eid-1"
    assert payload_for(candidate(VariantStatus.UNKNOWN))["data"]["state"] == "checking"


def test_every_event_a_variant_can_report_is_listed_once() -> None:
    assert len(set(EVENTS)) == len(EVENTS) == len(EVENT_FOR_STATUS)
    assert {"post.approved", "post.published", "post.failed", "post.cancelled"} <= set(EVENTS)
