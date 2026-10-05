# ADR 0018: Telegram alerts, digest and heartbeat

Status: accepted (2026-10-02)

## Decision

- Alerts are derived from the append-only `variant_events` trail (a variant entering `failed`, `expired` or `unknown`),
  not pushed from the use cases. Publishing therefore never depends on Telegram being reachable.
- Delivery is recorded in `publishing.alerts_sent` after Telegram accepts a message. A failed send is simply retried on
  the next sensor tick, in order. The cost is a possible duplicate if the process dies between the send and the
  record; we accept that over losing a failure alert.
- A first start only looks 24 hours back, so old history is not replayed.
- A failed Dagster run (a halted sync, a crashed job) is reported with the step's own error, at most once per job per
  hour.
- The 08:00 Berlin digest lists today's slots, yesterday's results with lateness, and warnings (tokens expiring
  within 14 days). One per Berlin day.
- The dead-man's switch is `HEARTBEAT_URL`, pinged by the one-minute `notifications` sensor, so it stops when the
  Dagster daemon stops.
- Telegram is optional: with no `TELEGRAM_CREDENTIALS_FILE` nothing is sent and nothing fails.

## Addendum: nightly login check

`token_health` runs at 03:45 Berlin (after the 03:30 token renewal) and reuses `check-setup`'s checks
(`composition.health_failures`): every `[FAIL]` line is sent in one Telegram message, once per Berlin day
per distinct set of problems (`health:<date>:<hash>` in `alerts_sent`). Found necessary when Meta removed
two permissions from a non-expiring token without any notice. The check never posts and never raises.

## Not built yet

Quota, disk and media-missing alerts; approval and assisted-publishing buttons (ADR 0014 dropped approvals).
