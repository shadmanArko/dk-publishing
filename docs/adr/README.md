# Architecture decision records

Each file records one decision so a later change argues against the original reasoning. Source: `docs/architecture.md`.

| # | Decision |
| --- | --- |
| 1 | [Dagster is the only scheduler](0001-dagster-is-the-only-scheduler.md) |
| 2 | [Postgres is both record and work queue](0002-postgres-is-both-record-and-work-queue.md) |
| 3 | [The Sheet is a one-way input with status written back](0003-the-sheet-is-a-one-way-input-with-status-written.md) |
| 4 | [Ports and adapters, with Dagster as a thin shell](0004-ports-and-adapters-with-dagster-as-a-thin-shell.md) |
| 5 | [Two-phase publish, with native scheduling chosen by capability](0005-two-phase-publish-with-native-scheduling-chosen.md) |
| 6 | [At-most-once publishing with reconciliation](0006-at-most-once-publishing-with-reconciliation.md) |
| 7 | [Retries are domain state](0007-retries-are-domain-state.md) |
| 8 | [Google Drive carries media from the PC](0008-google-drive-carries-media-from-the-pc.md) |
| 9 | [VPS disk plus tokenised Caddy URLs for media](0009-vps-disk-plus-tokenised-caddy-urls-for-media.md) |
| 10 | [SOPS and age for app secrets; AES-GCM in Postgres for tokens](0010-sops-and-age-for-app-secrets-aes-gcm-in-postgres.md) |
| 11 | [Assisted mode as a full Publisher](0011-assisted-mode-as-a-full-publisher.md) |
| 12 | [Default run launcher in one code-location container; own Dagster instance (option A)](0012-default-run-launcher-in-one-code-location-contai.md) |
| 12 | [Default run launcher in one code-location container; own Dagster instance (option A)](0012-default-run-launcher-in-one-code-location-contai.md) |
| 13 | [Separate repository, shared VPS and Postgres instance](0013-separate-repository-and-schema.md) |
| 14 | [Launch scope decisions](0014-scope-decisions.md) |
| 15 | [Forward-only SQL migrations, no downgrade](0015-forward-only-sql-migrations.md) |
| 16 | [Facebook adapter publishes directly; credentials come from a token file for now](0016-facebook-adapter-interim-credentials.md) |
| 17 | [Per-row delivery: native (the platform publishes) or direct (we publish)](0017-per-row-delivery-native-or-direct.md) |
| 18 | [Telegram alerts, digest and heartbeat](0018-telegram-alerts.md) |
| 19 | [Assisted publishing for TikTok](0019-assisted-publishing-for-tiktok.md) |
| 20 | [An HTTP API for other software, over the same engine](0020-http-api-for-other-software.md) |
| 21 | [Media is deleted when no post needs it, and uploads have limits](0021-media-retention-and-upload-limits.md) |
| 22 | [Posts from the API are approved in the Sheet's Calendar tab](0022-agent-posts-are-approved-in-the-calendar.md) |
| 23 | [Webhooks tell API clients when their posts change state](0023-webhooks-for-api-clients.md) |
