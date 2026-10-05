# ADR 0019: Assisted publishing for TikTok

Status: accepted (2026-10-02)

## Decision

TikTok is published in **assisted mode** (`mode: assisted`): at the slot, a Telegram card with the
caption and the video is sent to the owner, who posts it in the TikTok app. The variant is then
`published` in the database but shown in the Sheet as "sent to you", because the system cannot know
whether the person went on to post. Its external id starts with `assisted:` (the marker the Sheet
looks for); it has no URL.

A card is sent once per variant: the record lives in `publishing.alerts_sent` (`assist:<tenant>:<variant>`)
and `find_live` answers from it, so a crash between sending and recording is settled by reconcile
without a second card. A Telegram outage is `Retryable` (nothing was posted anywhere).

## Why not direct posting

TikTok's Content Posting API limits unaudited apps to private posts, asks that the user confirms each
post and chooses its privacy, and its app-review guidelines state "Apps must not be for private or
personal use". A self-use tool may never be approved. Direct posting is deferred until/unless an app
is approved; `tiktok.client_key/client_secret` in dk.json are reserved for it. The Sheet's TikTok tab
already asks for the per-post choices TikTok requires.

## Consequences

Any platform can be made assisted by adding rules (see `adapters/platforms/tiktok.py`) and a branch in
`assisted_build.py`; LinkedIn is the next candidate.

## Addendum: direct posting is built for the sandbox application

`adapters/platforms/tiktok_direct.py` and `tiktok_api.py` implement Login Kit sign-in
(`dk connect tiktok`) and Direct Post (creator info, init, chunked upload, status), so TikTok's
required sandbox demonstration can be recorded. `mode: live` selects it, `mode: assisted` stays the
default. Before the upload starts, the publish id is written to `alerts_sent`
(`tiktok:<tenant>:<variant>:<publish_id>`); `find_live` asks TikTok about those ids, and "no id" means
nothing was started. Tokens (24 h access, 365 d refresh) are renewed into `dk.json`.
