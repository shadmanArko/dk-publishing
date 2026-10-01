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
