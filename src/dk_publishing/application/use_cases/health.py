"""Telling the owner, before a post fails, that a login or permission has stopped working."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from html import escape

from dk_publishing.application.ports import Notifier
from dk_publishing.application.services import Services
from dk_publishing.domain.timezones import utc_to_berlin

GUIDE = "docs/setup/update-secrets.md"


def _clean(line: str) -> str:
    return line.removeprefix("[FAIL]").strip()


def health_message(failures: Sequence[str]) -> str:
    items = "\n".join(f"• {escape(_clean(f))}" for f in failures)
    return (
        "<b>🔑 A login needs attention</b>\n"
        f"{items}\n\n"
        f"<i>Posts to the affected platform will fail until this is fixed. Steps: {GUIDE}</i>"
    )


def report_health(
    services: Services, notifier: Notifier, failures: Sequence[str], tenant_id: str = "dk"
) -> bool:
    """Send one message listing what is broken. The same set of problems is reported at most once
    per Berlin day (a reminder each morning until it is fixed). False if nothing to say or already
    said today."""
    if not failures:
        return False
    now = services.clock.now()
    digest = hashlib.sha256("\n".join(sorted(failures)).encode()).hexdigest()[:12]
    key = f"health:{utc_to_berlin(now):%Y-%m-%d}:{digest}"
    with services.uow() as uow:
        if uow.alerts.was_sent(tenant_id, key):
            return False
    notifier.send(health_message(failures))
    with services.uow() as uow:
        uow.alerts.mark_sent(tenant_id, key, services.clock.now())
        uow.commit()
    return True
