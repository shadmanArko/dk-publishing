# TikTok

TikTok works in **assisted mode**: at the scheduled time the server sends **you** a Telegram message
with the caption ready to copy and the video attached. You open TikTok, upload the video, paste the
caption and tap Post. The Sheet shows the row as **sent to you**, with no link (nothing is posted by the system).

It is assisted rather than automatic because of TikTok's rules (see the end of this page).

## Set up (about 5 minutes)
1. **Telegram must work** ([telegram.md](telegram.md)). The cards arrive there.
2. In `dk.json`, fill `tiktok.handle` with your TikTok name, for example `@dhakakacchi`. It is only a label.
3. In `deploy/platforms.production.yaml`, `tiktok` must say `mode: assisted` (it does as shipped).
4. Create the account entry, then check:
   ```bash
   uv run --env-file .env dk account sync --name "Your Business"
   make check-setup
   ```
   On the server, `make deploy-secrets` does both (see [update-secrets.md](update-secrets.md)).

## Post a video
1. Put the video in the Drive media folder.
2. On the Sheet's **Posts** tab add a row (post_key, title, media file name, default time) and tick **ready**.
3. On the **TikTok** tab add a row for that `post_key`: `enabled` ticked, your account, the caption
   (blank uses the default caption), and **`privacy_level`**. TikTok never lets you skip the privacy
   choice, so the Sheet makes you pick one every time: `public`, `friends`, `followers` or `only_me`.
   Also tick or untick `allow_comments`, `allow_duet`, `allow_stitch`, and `commercial_disclosure`
   (tick it if the post promotes your own business).
4. At the time, Telegram sends two messages: the card (caption to tap-copy plus your choices) and the
   video. Save the video to your phone, open TikTok, upload it, paste the caption, set the options on the
   card, and post.

Videos over 50 MB cannot be attached on Telegram; the card then says so, and you take the file from the
Drive folder.

## Why it is not fully automatic
I read TikTok's developer rules (October 2026):
- **Direct posting needs an approved app, and approval needs an audit.** Until then every post made
  through the API is limited to "only me", and at most 5 users and private accounts are allowed.
- **TikTok asks that the person confirms each post and picks its privacy** in the app that posts.
- **The app-review guidelines say apps "must not be for private or personal use".** A tool that only
  posts to your own account may therefore be refused, and I cannot promise that it would be approved.

So assisted mode is the dependable route. If you still want to try for direct posting, the steps are
below; it costs nothing, but expect a possible "no".

## Optional: apply for direct posting
1. Create a TikTok developer account at [developers.tiktok.com](https://developers.tiktok.com), then go
   to **Manage apps** and **Connect an app**.
2. Fill in: app name, a 1024 x 1024 icon (JPEG or PNG, up to 5 MB), category, description, the
   **Terms of Service URL** and the **Privacy Policy URL**, and the platform (Web, with your website URL).
3. **Add products:** Login Kit and Content Posting API. For Content Posting API switch on **Direct Post**
   and request the `video.publish` permission. Set the **redirect URI** the form asks for.
4. Verify your website domain under **URL properties**.
5. Test in the **sandbox** first with your own account.
6. Under **App review**, explain how each product is used and upload a demo video (up to 5, 50 MB
   each) showing the whole flow, including the privacy choice. Submit, and wait.
7. Paste the **client key** and **client secret** into `dk.json` under `tiktok`. They are not used yet:
   direct posting for TikTok is not built, because it only makes sense once TikTok approves the app.
   Tell me if it is approved and I will build it; the same Sheet rows will keep working.
