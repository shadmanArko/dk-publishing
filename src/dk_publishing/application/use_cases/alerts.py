"""Telling the owner what needs a person: failures as they happen, and a morning digest.

Delivery is at-least-once in the safe direction: a message is recorded as sent only after Telegram
accepted it, so a Telegram outage delays alerts but never loses them. The record is what stops a
repeat.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from dk_publishing.application.ports import AlertEvent, DigestItem, Notifier, NotifyError
from dk_publishing.application.services import Services
from dk_publishing.domain.timezones import BERLIN, utc_to_berlin

LOOKBACK = timedelta(hours=24)  # a first start never floods with old history
BATCH = 20

_HEADLINE = {
    "failed": "❌ Failed",
    "expired": "⌛ Missed its slot",
    "unknown": "❓ Outcome uncertain",
}
_ADVICE = {
    "failed": "It was not published. Check the platform, then edit the row to try again.",
    "expired": "It was too late to publish safely, so it was skipped.",
    "unknown": "It is being checked against the platform; it will not be posted twice.",
}


@dataclass
class AlertReport:
    sent: int = 0
    failed: list[str] = field(default_factory=list)


def _when(moment: datetime) -> str:
    return f"{utc_to_berlin(moment):%d.%m. %H:%M}"


def _title(item: DigestItem) -> str:
    return escape(item.title) or "(untitled)"


def format_event(event: AlertEvent) -> str:
    head = _HEADLINE.get(event.status, event.status)
    title = escape(event.title) or "(untitled)"
    return (
        f"<b>{head}</b>: {title}\n"
        f"{escape(event.platform)} · {escape(event.account)} · slot {_when(event.publish_at)}\n"
        f"{escape(event.reason)}\n"
        f"<i>{_ADVICE.get(event.status, '')}</i>"
    )


def send_alerts(services: Services, notifier: Notifier, tenant_id: str = "dk") -> AlertReport:
    """Send every failed, expired or uncertain transition not yet reported, oldest first."""
    now = services.clock.now()
    with services.uow() as uow:
        events = uow.alerts.unsent_events(tenant_id, now - LOOKBACK, BATCH)
    report = AlertReport()
    for event in events:
        try:
            notifier.send(format_event(event))
        except NotifyError as exc:
            report.failed.append(str(exc))
            break  # the same outage would hit the rest; try again next tick, in order
        with services.uow() as uow:
            uow.alerts.mark_sent(tenant_id, event.key, services.clock.now())
            uow.commit()
        report.sent += 1
    return report


def send_run_failure(
    services: Services,
    notifier: Notifier,
    *,
    run_id: str,
    job: str,
    error: str,
    tenant_id: str = "dk",
) -> bool:
    """Report a failed Dagster run (a halted sync, a crashed job). At most one report per job per
    hour, so a sync that fails every 15 minutes is one message, not four. False if suppressed."""
    now = services.clock.now()
    key = f"run:{job}:{now:%Y%m%d%H}"
    with services.uow() as uow:
        if uow.alerts.was_sent(tenant_id, key):
            return False
    notifier.send(
        f"<b>⚠️ Job failed</b>: {escape(job)}\n{escape(error[:600])}\n"
        f"<i>Run {escape(run_id[:8])}. Open Dagster for the log.</i>"
    )
    with services.uow() as uow:
        uow.alerts.mark_sent(tenant_id, key, services.clock.now())
        uow.commit()
    return True


def digest_text(
    *,
    yesterday: Sequence[DigestItem],
    today: Sequence[DigestItem],
    warnings: Sequence[str],
) -> str:
    published = [i for i in yesterday if i.status == "published"]
    problems = [i for i in yesterday if i.status != "published"]
    lines = ["<b>Good morning. Today's posts</b>"]
    if today:
        lines += [
            f"{utc_to_berlin(i.publish_at):%H:%M} {escape(i.platform)} · {_title(i)}" for i in today
        ]
    else:
        lines.append("Nothing scheduled today.")
    lines.append("")
    lines.append(f"<b>Yesterday</b>: {len(published)} published, {len(problems)} not")
    for item in published:
        assert item.published_at is not None
        late = int((item.published_at - item.publish_at).total_seconds())
        lines.append(f"✅ {escape(item.platform)} · {escape(item.title)} ({late}s after the slot)")
    for item in problems:
        lines.append(f"❌ {escape(item.platform)} · {escape(item.title)}: {escape(item.status)}")
    if warnings:
        lines.append("")
        lines.append("<b>Needs you</b>")
        lines += [f"⚠️ {escape(w)}" for w in warnings]
    return "\n".join(lines)


def send_digest(
    services: Services,
    notifier: Notifier,
    warnings: Sequence[str] = (),
    tenant_id: str = "dk",
) -> bool:
    """Today's slots, yesterday's results and anything waiting on a person. Once per Berlin day."""
    now = services.clock.now()
    local = utc_to_berlin(now)
    key = f"digest:{local:%Y-%m-%d}"
    start_today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_yesterday = (start_today - timedelta(days=1)).astimezone(BERLIN)
    with services.uow() as uow:
        if uow.alerts.was_sent(tenant_id, key):
            return False
        yesterday = uow.alerts.results(tenant_id, start_yesterday, start_today)
        today = uow.alerts.upcoming(tenant_id, start_today, start_today + timedelta(days=1))
    notifier.send(digest_text(yesterday=yesterday, today=today, warnings=warnings))
    with services.uow() as uow:
        uow.alerts.mark_sent(tenant_id, key, services.clock.now())
        uow.commit()
    return True
