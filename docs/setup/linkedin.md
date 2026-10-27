# LinkedIn

LinkedIn works in **assisted mode**: at the scheduled time the server sends **you** a Telegram message
with the caption ready to copy, and the images or the video attached. You open LinkedIn, start a post,
add the files, paste the caption and tap Post. The Sheet shows the row as **sent to you**, with no link.

It is assisted because posting to a company Page through LinkedIn's API needs the **Community Management
API**, and LinkedIn grants it only after reviewing the organisation and the app (a registered business, a
verified Page admin, a demo). If that is approved later, the adapter is swapped and nothing else changes.

## Set up (about 5 minutes)
1. **Telegram must work** ([telegram.md](telegram.md)). The cards arrive there.
2. In `dk.json`, fill `linkedin.handle` with the Page or profile name. It is only a label.
3. In `deploy/platforms.production.yaml`, `linkedin` must say `mode: assisted` (it does as shipped).
4. Create the account entry, add the Sheet's LinkedIn tab and check:
   ```bash
   uv run --env-file .env dk account sync --name "Your Business"
   make sheet-init
   make check-setup
   ```

## Post
1. Put the images (JPG, PNG or GIF, up to 9) or one video (MP4 or MOV) in the Drive media folder, or none for a
   text post.
2. On the **LinkedIn** tab add a row for the `post_key`: `enabled` ticked, your account, the caption (up to 3000
   characters), and optionally `post_as`: `page` (the company Page, the default) or `profile` (you post from your
   own profile, which usually reaches more people).
3. At the time, Telegram sends the card (caption to tap-copy) and then the files. Open LinkedIn, add them in
   order, paste the caption and post.

Put links in the **first comment**, not in the text: LinkedIn shows posts with links to fewer people.
Images over 10 MB cannot be attached on Telegram.
