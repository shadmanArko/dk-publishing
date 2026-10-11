"""Telling an API client when its posts change state (ADR 0023).

Three steps, each safe to repeat, none of them on the publishing path:
  1. `enqueue_events`: every outcome (approved, published, failed, ...) of a client's posts becomes
     one queued message per live endpoint of that client's key. A unique key makes a rerun a no-op.
  2. `deliver_due`: claim what is due (leased, so two workers never send the same message at once),
     send it signed, record the answer. A failure is retried later with growing gaps, then given up.
  3. The owner is told on Telegram, once, about a message that was given up on and about an endpoint
     the client said is gone (HTTP 410).
Delivery is at-least-once: a client may get a message twice (a lost answer), never none silently.
The message id is stable, so the client can ignore a repeat.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from html import escape
from typing import Any
from urllib.parse import urlparse

from dk_publishing.application.ports import (
    ApiKey,
    Notifier,
    NotifyError,
    SendResult,
    WebhookCandidate,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookSender,
    WebhookUrlRejected,
)
from dk_publishing.application.services import ApiServices, Services
from dk_publishing.application.use_cases.api_posts import STATE, ApiError, Problem
from dk_publishing.domain.status import VariantStatus
from dk_publishing.domain.webhooks import EVENTS, PING, body_bytes

MAX_ENDPOINTS = 3  # per key
MAX_URL = 2000
LOOKBACK = timedelta(days=7)  # a first start never floods a client with old history
ENQUEUE_BATCH = 200
SEND_BATCH = 25  # per run: each send may take up to its 10 s timeout
LEASE = timedelta(minutes=5)
# After the n-th failed attempt wait RETRY_GAPS[n-1]; the MAX_ATTEMPTS-th failure gives up.
RETRY_GAPS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
    timedelta(hours=24),
)
MAX_ATTEMPTS = len(RETRY_GAPS) + 1
SECRET_PREFIX = "whsec_"


@dataclass
class DeliveryReport:
    queued: int = 0
    delivered: int = 0
    retrying: int = 0
    dead: int = 0


# --- managing endpoints (the API) -----------------------------------------------------------------


def register_endpoint(
    api: ApiServices, key: ApiKey, url: str, events: Sequence[str]
) -> tuple[WebhookEndpoint, str]:
    """Returns the endpoint and its signing secret. The secret is shown this once."""
    problems: list[Problem] = []
    if len(url) > MAX_URL:
        problems.append(Problem("url", "too_long", f"At most {MAX_URL} characters.", limit=MAX_URL))
    else:
        problems += _check_url(api, url)
    unknown = [e for e in events if e not in EVENTS]
    if unknown:
        problems.append(
            Problem(
                "events",
                "not_allowed",
                f"Unknown event(s): {', '.join(unknown)}. Leave `events` out to receive all.",
                allowed=EVENTS,
            )
        )
    if problems:
        raise ApiError("invalid", "The webhook was not created.", problems)
    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    with api.core.uow() as uow:
        if len(uow.webhooks.endpoints(api.tenant_id, key.id)) >= MAX_ENDPOINTS:
            raise ApiError(
                "conflict",
                f"A key may have {MAX_ENDPOINTS} webhooks. Delete one first "
                "(DELETE /v1/webhooks/{id}).",
            )
        endpoint = uow.webhooks.add_endpoint(
            api.tenant_id, key.id, url, secret, sorted(set(events))
        )
        uow.commit()
    return endpoint, secret


def _check_url(api: ApiServices, url: str) -> list[Problem]:
    if api.webhooks is None:
        return [Problem("url", "webhooks_off", "Webhooks are not enabled on this server.")]
    try:
        api.webhooks.check(url)
    except WebhookUrlRejected as exc:
        return [Problem("url", exc.code, exc.message)]
    return []


def list_endpoints(api: ApiServices, key: ApiKey) -> list[WebhookEndpoint]:
    with api.core.uow() as uow:
        return uow.webhooks.endpoints(api.tenant_id, key.id)


def delete_endpoint(api: ApiServices, key: ApiKey, endpoint_id: str) -> None:
    with api.core.uow() as uow:
        deleted = _is_uuid(endpoint_id) and uow.webhooks.delete_endpoint(
            api.tenant_id, key.id, endpoint_id
        )
        uow.commit()
    if not deleted:
        raise ApiError("not_found", "No such webhook for this key.")


def list_deliveries(
    api: ApiServices, key: ApiKey, endpoint_id: str, limit: int = 50
) -> list[WebhookDelivery]:
    with api.core.uow() as uow:
        if not _is_uuid(endpoint_id) or not any(
            e.id == endpoint_id for e in uow.webhooks.endpoints(api.tenant_id, key.id)
        ):
            raise ApiError("not_found", "No such webhook for this key.")
        return uow.webhooks.deliveries(api.tenant_id, key.id, endpoint_id, limit)


def ping(api: ApiServices, key: ApiKey, endpoint_id: str) -> SendResult:
    """Send one test message now and report the answer, so a client can check its receiver."""
    with api.core.uow() as uow:
        found = (
            uow.webhooks.endpoint_secret(api.tenant_id, key.id, endpoint_id)
            if _is_uuid(endpoint_id)
            else None
        )
    if found is None or api.webhooks is None:
        raise ApiError("not_found", "No such active webhook for this key.")
    now = api.core.clock.now()
    event_id = f"evt_ping_{uuid.uuid4().hex}"
    payload = {
        "id": event_id,
        "type": PING,
        "created_at": now.isoformat(),
        "data": {"message": "A test message from DK Publishing. Reply with any 2xx status."},
    }
    return api.webhooks.send(
        found[0], found[1], delivery_id=event_id, event_id=event_id, event_type=PING,
        body=body_bytes(payload), now=now,
    )  # fmt: skip


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


# --- the messages ---------------------------------------------------------------------------------


def event_id(candidate: WebhookCandidate) -> str:
    """Stable for one state change, the same for every endpoint and every retry."""
    return f"evt_{candidate.variant_id}_{candidate.seq}"


def payload_for(candidate: WebhookCandidate) -> dict[str, Any]:
    status = VariantStatus(candidate.status)
    published = status is VariantStatus.PUBLISHED
    return {
        "id": event_id(candidate),
        "type": candidate.event_type,
        "created_at": candidate.at.isoformat(),
        "data": {
            "post_key": candidate.post_key,
            "variant_id": candidate.variant_id,
            "platform": candidate.platform,
            "account": candidate.account,
            "format": candidate.format,
            "state": STATE[status],
            "status": status.value,
            "slot": candidate.slot.isoformat(),
            "live_url": candidate.live_url if published else None,
            "handed_over": candidate.handed_over,
            "reason": candidate.reason,
        },
    }


def enqueue_events(services: Services, tenant_id: str = "dk") -> int:
    """Queue every state change an endpoint has not been told about. Safe to run at any time."""
    now = services.clock.now()
    queued = 0
    with services.uow() as uow:
        for candidate in uow.webhooks.candidates(tenant_id, now - LOOKBACK, ENQUEUE_BATCH):
            if uow.webhooks.queue(
                tenant_id, candidate, event_id(candidate), payload_for(candidate), now
            ):
                queued += 1
        uow.commit()
    return queued


def retry_at(delivery: WebhookDelivery, now: Any) -> Any:
    """When to try again after this attempt failed, or None to give up."""
    failed = delivery.attempts + 1
    return None if failed >= MAX_ATTEMPTS else now + RETRY_GAPS[failed - 1]


def deliver_due(
    services: Services,
    sender: WebhookSender,
    notifier: Notifier | None = None,
    tenant_id: str = "dk",
) -> DeliveryReport:
    report = DeliveryReport(queued=enqueue_events(services, tenant_id))
    now = services.clock.now()
    with services.uow() as uow:
        claimed = uow.webhooks.claim_due(now, SEND_BATCH, LEASE)
        uow.commit()  # the lease is what keeps a second worker off these messages
    for delivery in claimed:
        sent_at = services.clock.now()
        result = sender.send(
            delivery.url, delivery.secret, delivery_id=delivery.id, event_id=delivery.event_id,
            event_type=delivery.event_type, body=body_bytes(delivery.payload), now=sent_at,
        )  # fmt: skip
        again = None if result.ok or result.gone else retry_at(delivery, sent_at)
        with services.uow() as uow:
            uow.webhooks.record_attempt(
                delivery.id, sent_at, ok=result.ok, http_status=result.http_status,
                error=result.error, retry_at=again,
            )  # fmt: skip
            if result.gone:
                uow.webhooks.disable_endpoint(
                    delivery.endpoint_id, "The receiver answered HTTP 410 (gone)."
                )
            uow.commit()
        if result.ok:
            report.delivered += 1
        elif again is not None:
            report.retrying += 1
        else:
            report.dead += 1
            _tell_owner(services, notifier, delivery, result, tenant_id)
    return report


def _tell_owner(
    services: Services,
    notifier: Notifier | None,
    delivery: WebhookDelivery,
    result: SendResult,
    tenant_id: str,
) -> None:
    if notifier is None:
        return
    key = f"webhook-dead:{delivery.id}"
    with services.uow() as uow:
        if uow.alerts.was_sent(tenant_id, key):
            return
    host = escape(urlparse(delivery.url).hostname or "?")
    why = "it said the address is gone (HTTP 410)" if result.gone else escape(result.error or "?")
    post_key = escape(str(delivery.payload.get("data", {}).get("post_key", "")))
    text = (
        "<b>📮 A webhook message could not be delivered</b>\n"
        f"{escape(delivery.event_type)} for {post_key} → {host}\n"
        f"Given up after {delivery.attempts + 1} attempt(s): {why}.\n"
        "<i>The post itself is unaffected; the client can still read its status "
        "with GET /v1/posts/{post_key}.</i>"
    )
    try:
        notifier.send(text)
    except NotifyError:
        return  # not recorded as sent: tried again the next time the owner could be told
    with services.uow() as uow:
        uow.alerts.mark_sent(tenant_id, key, services.clock.now())
        uow.commit()


def has_work(services: Services) -> bool:
    """Cheap check for the sensor: is anything queued and due, or about to be queued?"""
    with services.uow() as uow:
        return bool(
            uow.webhooks.has_due(services.clock.now())
            or uow.webhooks.candidates("dk", services.clock.now() - LOOKBACK, 1)
        )
