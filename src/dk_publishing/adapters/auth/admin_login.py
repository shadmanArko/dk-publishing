"""Sign-in with the admin panel's own email and password (ADR 0026).

The password is checked by the admin backend, never here: this adapter forwards the pair once over
https to its login address, reads whether it was accepted, then closes the admin session that
check opened (so nothing is left behind there). Nothing is stored or logged, and a failure to
reach the backend is `LoginUnavailable`, never a yes.
"""

from __future__ import annotations

from contextlib import suppress
from urllib.parse import urlparse

import httpx

from dk_publishing.application.ports import LoginUnavailable

TIMEOUT_SECONDS = 8.0


def login_url_ok(url: str) -> bool:
    """https (or local, for tests), and the path ends in /login."""
    parts = urlparse(url)
    local = parts.hostname in ("localhost", "127.0.0.1")
    return (
        parts.scheme in ("https", "http")
        and bool(parts.hostname)
        and parts.path.endswith("/login")
        and (parts.scheme == "https" or local)
    )


class AdminBackendLogin:
    def __init__(self, login_url: str, transport: httpx.BaseTransport | None = None) -> None:
        self._url = login_url.strip()
        self._transport = transport

    def verify(self, email: str, password: str) -> str | None:
        try:
            with httpx.Client(
                transport=self._transport, timeout=TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                answer = client.post(self._url, json={"email": email, "password": password})
                if answer.status_code in (400, 401, 403, 422):
                    return None
                if answer.status_code != 200:
                    raise LoginUnavailable(f"the admin login answered {answer.status_code}")
                body = answer.json()
                token, admin = body.get("token"), body.get("adminUser") or {}
                confirmed = str(admin.get("email", "")).strip().lower()
                if isinstance(token, str) and token:
                    self._close(client, token)
        except (httpx.HTTPError, ValueError) as exc:
            raise LoginUnavailable("the admin login could not be reached") from exc
        return confirmed or None

    def _close(self, client: httpx.Client, token: str) -> None:
        """Best effort: end the admin session the check opened. A failure here changes nothing."""
        with suppress(httpx.HTTPError):
            client.post(
                self._url.removesuffix("/login") + "/logout",
                headers={"Authorization": f"Bearer {token}"},
            )
