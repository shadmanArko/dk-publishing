# dk-publishing

Publishes your approved posts from one Google Sheet to Facebook, Instagram, Threads and YouTube,
and hands TikTok posts to you on Telegram (more later), on time and never twice. Tick **ready** in the Sheet; the system does the rest and
tells you on Telegram if anything needs you. It is business-agnostic: your brand lives only in the
Sheet and in `dk.json`.

## What you need

| Need | Why | Guide |
| --- | --- | --- |
| A server or computer with Docker | runs everything | below |
| A Google account | the Sheet (input) and a Drive folder (media) | [Google](docs/setup/google.md) |
| Facebook Page, Instagram professional account, Threads account | where posts go | [Meta](docs/setup/meta.md) |
| A YouTube channel | video posts | [YouTube](docs/setup/youtube.md) |
| A Telegram account | alerts and the 08:00 digest (optional) | [Telegram](docs/setup/telegram.md) |
| TikTok | posts are handed to you on Telegram | [TikTok](docs/setup/tiktok.md) |
| LinkedIn, X, Reddit | not supported yet | [Status](docs/setup/other-platforms.md) |
| A server (for production) | runs it all day, serves media links | [Deploy](docs/setup/deploy.md) |
| A token or key stopped working | change it and send it to the server | [Update a token or key](docs/setup/update-secrets.md) |

Use only the platforms you want. Anything left empty is skipped.

## Set up (once)

```bash
cp .env.example .env     # holds only DATABASE_URL and DK_CONFIG_FILE
make install
make init               # creates dk.json, the ONE file for every id, key and token
```

1. Add the `DK_CONFIG_FILE=...` line that `make init` prints to `.env`.
2. Open `dk.json` and fill in the sections you need, using the guides above.
   Each section's `_help` line names its guide. Keep `dk.json` and `google-key.json` in the same
   folder, outside git.
3. `make check-setup` tests every filled-in section and says what is missing. It posts nothing.
4. Start the database and the system:

```bash
make db-up && make migrate
uv run --env-file .env dk account sync --name "Your Business"   # accounts from the ids in dk.json
make sheet-init          # builds the Sheet's tabs, dropdowns and instructions
make dagster-dev         # open http://localhost:3000
```

## Prove it works with a real post

Each command publishes one small real post, then reads it back from the platform. Delete the post
afterwards (YouTube videos are private). Without `--yes` nothing is posted.

```bash
uv run --env-file .env dk live-test facebook --yes
uv run --env-file .env dk live-test threads --yes
uv run --env-file .env dk live-test instagram --video clip.mp4 --yes
uv run --env-file .env dk live-test youtube --video clip.mp4 --yes
```

## Run it on a server

`deploy/deploy.sh root@SERVER media.yourbusiness.com --secrets --account-name "Your Business"` after a one-time `deploy/bootstrap.sh`.
Steps and what runs by itself: [docs/setup/deploy.md](docs/setup/deploy.md).

## When a token or login stops working

Edit `dk.json` on your computer, run `make check-setup`, then `make deploy-secrets`.
Short steps and a problem-to-fix table: [docs/setup/update-secrets.md](docs/setup/update-secrets.md).

## Everyday use

1. Add a media file to the Drive folder.
2. Add a row in the platform's tab of the Sheet: caption, media, `slot` (Berlin time), `account`.
3. Tick **ready**. Its `status` and `live_url` update in the Sheet. Edit or untick to change or cancel.

Set `delivery` to `native` on Facebook/YouTube rows to let the platform hold the post itself
([why](docs/adr/0017-per-row-delivery-native-or-direct.md)).

## Switching a platform on

Platforms are `dry_run` (rehearsal, posts nothing) until you set `mode: live` in
[`config/platforms.yaml`](config/platforms.yaml). Do that only after `live-test` succeeds for it.

## Commands

| Command | Does |
| --- | --- |
| `make init` / `make check-setup` | create / test `dk.json` |
| `make meta-check`, `make meta-refresh` | test / renew Meta tokens |
| `make connect-youtube` | one-time YouTube login |
| `make telegram-test` | send yourself a test message |
| `make check` | full quality gate for developers |

More: [architecture](docs/architecture.md) · [decisions](docs/adr/README.md) · [working notes](CLAUDE.md)
