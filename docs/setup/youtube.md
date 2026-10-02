# YouTube

Fill `dk.json` → `youtube`: `client_id` and `client_secret` by hand; `refresh_token` and `channel_id`
are filled by one command.

## 1. OAuth client
1. [console.cloud.google.com](https://console.cloud.google.com) → pick or create a project.
2. **APIs & Services → Library**: enable **YouTube Data API v3**.
3. **OAuth consent screen**: choose **External**, fill the required names, add yourself as a test user,
   then **Publish app** (status "In production"). Left in "Testing", Google expires the login after 7 days.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**. Copy the client id and secret
   into `youtube.client_id` and `youtube.client_secret`.

## 2. Connect (once)
```bash
make connect-youtube     # opens your browser; sign in as the channel's owner and allow
make check-youtube
uv run --env-file .env dk live-test youtube --video clip.mp4 --yes
```
When Google shows "this app isn't verified", choose **Advanced → continue**; it is your own app.

## Good to know
- Google's documentation says videos uploaded through an **unaudited** API project are locked to
  **private**. In our own test (October 2026) a video set to public stayed public, so you may not be
  affected. Check by opening the video link in a private browser window. If your videos do come out
  private (the system reports it instead of pretending), request Google's compliance audit: fill in the
  "Audit and Quota Extension Form" (search for it on Google's "YouTube API Services" pages). It is free,
  needs no business licence, and asks for your project number, what the app does, and a link to a
  privacy policy; answers take days to weeks.
- Each upload costs about 1,600 of the default 10,000 daily quota units (around six uploads a day).
- Every video must declare whether it is made for kids: use the `made_for_kids` column (yes/no).
