# Meta: Facebook, Instagram and Threads

Fill `dk.json` → `meta`. Everything is tested by `make meta-check` (it never posts).
Menu names in Meta's tools change often; follow the intent if a label differs.

## Before you start
- A **Facebook Page** you are an admin of (a personal profile cannot be used).
- An **Instagram professional (Business or Creator) account linked to that Page**.
- A **Threads** account (for Threads).

## 1. Create the app (once)
1. [developers.facebook.com](https://developers.facebook.com) → **My Apps → Create App**. Choose the
   option for managing business pages/other use cases, and give it a name.
2. Copy **App ID** into `meta.app_id` and **App secret** (Settings → Basic) into `meta.app_secret`.
3. Add the products you need: **Facebook Login for Business / Instagram**, and **Threads** (for Threads).
4. While the app is in development mode only people with a role on the app can use it. That is fine for
   your own accounts: add yourself under **App roles**.

## 2. Facebook (and Instagram)
1. Find your **Page ID**: Page → About → Page transparency, or the number in the Page's URL. Put it in
   `meta.facebook.page_id`.
2. Open **Tools → Graph API Explorer**, choose your app, and request these permissions:
   `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`,
   `instagram_content_publish`. Click **Generate Access Token** and approve for your Page.
3. Paste that token into `meta.facebook.access_token`, then run:
   ```bash
   make meta-page-token     # swaps it for the Page's own token and stores it
   make meta-check
   ```
4. **Instagram:** find the Instagram account id with `GET /{page-id}?fields=instagram_business_account`
   in the Explorer and put it in `meta.instagram.account_id`. Leave `meta.instagram.access_token`
   empty: the Page token is used.

## 3. Threads
1. In the app, open the Threads product and add yourself as a **Threads tester**; accept the invite in
   the Threads app (Settings → Account → Website permissions → Invites).
2. Generate a Threads user token with `threads_basic` and `threads_content_publish`. Put it in
   `meta.threads.access_token`, and your Threads user id in `meta.threads.user_id`.
3. `make meta-refresh` turns it into a 60-day token and then renews it. The system renews it for you
   afterwards; the morning digest warns if it gets close to expiring.

## 4. Prove it
```bash
make meta-check
uv run --env-file .env dk live-test facebook --yes
uv run --env-file .env dk live-test threads --yes
uv run --env-file .env dk live-test instagram --video clip.mp4 --yes   # a vertical 9:16 mp4, 3-90 s
```
Photos on Instagram and Threads need a public web address for the file, which exists once the server
is deployed (`media.public_base_url`, `media.public_dir`). Text and videos work before that.

## Troubleshooting
| Message | Fix |
| --- | --- |
| `(#10)` / permission errors | the token lacks a permission above; regenerate it with all of them |
| "belongs to the user, not the Page" | run `make meta-page-token` |
| `Session has expired` / code 190 | generate a new token and paste it; no restart needed |
