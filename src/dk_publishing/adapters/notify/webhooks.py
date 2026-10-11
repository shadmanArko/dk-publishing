"""Sending signed webhook messages to a client's address (ADR 0023).

The address was chosen by a client, so it is untrusted: https only, and it must resolve to public
hosts only. The connection goes to the address that was just checked (the Host header and the TLS
name stay the client's), so a DNS answer that changes between the check and the call cannot reach a
private network. Redirects are never followed. No error carries the address or the response body.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse, urlunparse

import httpx

from dk_publishing.adapters.net import Resolver, is_public, resolve_host
from dk_publishing.application.ports import SendResult, WebhookUrlRejected
from dk_publishing.domain.webhooks import sign

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
USER_AGENT = "DK-Publishing-Webhooks/1"


class HttpWebhookSender:
    def __init__(
        self, *, transport: httpx.BaseTransport | None = None, resolver: Resolver = resolve_host
    ) -> None:
        self._transport = transport
        self._resolver = resolver

    def check(self, url: str) -> None:
        self._addresses(url)

    def _addresses(self, url: str) -> tuple[str, list[str]]:
        parts = urlparse(url)
        if parts.scheme != "https" or not parts.hostname:
            raise WebhookUrlRejected("bad_url", "Only https addresses are accepted.")
        if parts.username or parts.password:
            raise WebhookUrlRejected(
                "bad_url", "The address must not contain a user name or password."
            )
        addresses = list(self._resolver(parts.hostname))
        if not addresses or not all(is_public(a) for a in addresses):
            raise WebhookUrlRejected(
                "bad_url", "The address does not point to a public host on the internet."
            )
        return parts.hostname, addresses

    def send(
        self,
        url: str,
        secret: str,
        *,
        delivery_id: str,
        event_id: str,
        event_type: str,
        body: bytes,
        now: datetime,
    ) -> SendResult:
        try:
            host, addresses = self._addresses(url)
        except WebhookUrlRejected as exc:
            return SendResult(False, None, exc.message)
        parts = urlparse(url)
        ip = addresses[0]
        netloc = f"[{ip}]" if ":" in ip else ip
        if parts.port:
            netloc += f":{parts.port}"
        pinned = urlunparse(parts._replace(netloc=netloc))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Host": f"{host}:{parts.port}" if parts.port else host,
            "X-DK-Event": event_type,
            "X-DK-Event-Id": event_id,
            "X-DK-Delivery": delivery_id,
            "X-DK-Signature": sign(secret, int(now.timestamp()), body),
        }
        try:
            with httpx.Client(
                transport=self._transport, timeout=TIMEOUT, follow_redirects=False
            ) as http:
                response = http.post(
                    pinned, content=body, headers=headers, extensions={"sni_hostname": host}
                )
        except httpx.TimeoutException:
            return SendResult(False, None, "timed out")
        except httpx.HTTPError:
            return SendResult(False, None, "could not connect")
        status = response.status_code
        if 200 <= status < 300:
            return SendResult(True, status)
        if 300 <= status < 400:
            return SendResult(False, status, f"HTTP {status}: redirects are not followed")
        return SendResult(False, status, f"HTTP {status}", gone=status == 410)
