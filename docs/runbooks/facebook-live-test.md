# Runbook: your first real Facebook post

> Superseded for setup by [`docs/setup/meta.md`](../setup/meta.md) and `dk live-test facebook --yes`; kept for the Sheet-driven steps.

Posts made this way are REAL and public on the Page. Delete them by hand afterwards.

1. **Token.** In Meta's Graph API Explorer, pick the new app, request `pages_show_list`,
   `pages_read_engagement` and `pages_manage_posts`, generate a user token, then call
   `GET /me/accounts` and copy the **Page** access token for the Page. Save only that token in a file
   outside this repo (for example `~/.config/dk-publishing/fb-page-token`), then `chmod 600` it.
2. **.env** (git-ignored): set `FACEBOOK_PAGE_ID` and `FACEBOOK_PAGE_TOKEN_FILE` (an absolute path).
3. **Account.** `uv run --env-file .env dk account add facebook "<Page name>" --external-id <PAGE_ID>`
   and use exactly that name in the Facebook row's `account` cell.
4. **Go live.** In `config/platforms.yaml` change `facebook: mode: dry_run` to `mode: live`. Leave every
   other platform on dry_run. (Revert to `dry_run` to switch it off again.)
5. **Post.** Put a short test video in the Drive folder, fill a Facebook row (`format: video`, caption,
   a `slot` a few minutes ahead), tick `ready`. With `make dagster-dev` running, it publishes at the slot.
6. **Check.** The row's `live_url` opens the post. If something fails, `last_error` says why in plain words.

If the token expires (the row shows "Session has expired"), generate a new one into the same file; no restart.

## Native scheduling (optional)

In the Facebook row set `delivery` to `native`: Facebook holds the post and publishes it itself at the slot,
even if this system is off. The slot must be at least 10 minutes away. You will see the post under the Page's
Scheduled posts in Meta Business Suite. Editing, unticking or deleting the row removes it from there first.
Leave `delivery` blank (or `direct`) and this system publishes at the slot instead.
