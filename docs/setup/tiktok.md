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

## Direct posting: the application, step by step
This is optional and may be refused (see above). Assisted mode keeps working while you wait.

### 1. The app on TikTok (done once)
1. [developers.tiktok.com](https://developers.tiktok.com): create an **organization**, then **Create app**
   (type **Other**). Fill in name, 1024 x 1024 icon, category, description, the **Terms of Service**
   and **Privacy Policy** URLs of your website, platform **Web** and your website address.
2. Verify the domain under **URL properties**: choose **Domain**, add the TXT record TikTok shows
   at your DNS provider, click **Verify**.
3. **Products:** add **Login Kit** and **Content Posting API** (switch on **Direct Post**).
   **Scopes:** only `user.info.basic` and `video.publish`.
4. **Redirect URI** (Login Kit): `https://media.<your domain>/tiktok/callback`. The server answers that
   address with a short "copy this address" page.

### 2. The sandbox
1. In the app, open the **Sandbox** tab, **Create Sandbox**, tick **Clone from Production**.
2. Check the products, scopes and redirect URI were copied. Under **Sandbox settings** add your own
   TikTok account as a **target user**. Click **Apply changes**.
3. Reveal the sandbox **client key** and **client secret** and put them in `dk.json`:
   ```json
   "tiktok": {
     "handle": "@yourname",
     "client_key": "...", "client_secret": "...",
     "redirect_uri": "https://media.yourbusiness.com/tiktok/callback"
   }
   ```
4. Sign in once: `make connect-tiktok`. The browser opens TikTok; allow the app. TikTok then lands on
   your redirect address: copy the **full address** of that page and paste it into the terminal. It
   prints `connected to TikTok as @yourname` and what privacy levels the account may use.
5. Try one post (private in the sandbox, which is TikTok's rule):
   ```bash
   uv run --env-file .env dk live-test tiktok --video clip.mp4 --yes
   ```
6. To post from the Sheet instead of Telegram, set `tiktok` to `mode: live` in
   `deploy/platforms.production.yaml` and `make deploy-secrets`. Back to assisted: `mode: assisted`.

### 3. The demo video and the review
TikTok wants one video (mp4 or mov, under 50 MB) of the whole flow. Record your screen, with your
website's address visible at the start, and show in this order:
1. The Google Sheet: a TikTok row with the video name, caption, **`privacy_level`**, the comment/duet/
   stitch boxes, and **ready** ticked (this is the owner's consent for that post).
2. The sign-in: `make connect-tiktok`, TikTok's page asking to allow "DK Publishing" with its
   permissions, you tapping Allow, and the terminal showing the connected account.
3. The post being made at the slot (the Sheet row changes to published), then the video appearing in
   the TikTok app.

In the app's **App review** box paste the text from [tiktok-review-text.md](tiktok-review-text.md),
upload the video, and **Submit for review**. Then wait; TikTok does not publish a review time.

### 4. If TikTok approves
Paste the **production** client key and secret into `dk.json`, run `make connect-tiktok` again, set the
account in TikTok to public, and use `privacy_level: public` in the Sheet. Posts then go out by themselves.
