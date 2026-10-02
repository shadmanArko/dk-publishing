# Change a token, key or password

Do this when a platform stops accepting a login: a Telegram alert such as "Session has expired" or
"token revoked", or `make check-setup` printing `[FAIL]` for one platform. Everything is edited on
**your computer** and then sent to the server.

> Never edit `dk.json` on the server. The server rewrites its own copy when it renews a token, and
> your next deploy replaces it.

## One-time: tell the deploy command where your server is
```bash
cp deploy/server.conf.example deploy/server.conf
```
Open `deploy/server.conf` and fill in your server, for example:
```
SERVER=root@203.0.113.10
DOMAIN=media.yourbusiness.com
ACCOUNT_NAME="Your Business"
```
The file stays on your computer (git ignores it).

## The 5 steps
**1. Get the new token or login** with the matching guide:
[Facebook / Instagram / Threads](meta.md) · [YouTube](youtube.md) · [Telegram](telegram.md) · [Google](google.md)

**2. Put it into `dk.json`:**
```bash
open -a TextEdit ~/.config/dk-publishing/dk.json
```
Paste the new value between the quotes, for example:
```json
"telegram": { "bot_token": "123456:NEW-TOKEN", "chat_id": "987654321" }
```
Save, then **close the editor window** (an old open window overwrites newer changes when saved).

**3. Test on your computer:**
```bash
make check-setup
```
That platform's line must say `[ok]`.

**4. Send it to the server:**
```bash
make deploy-secrets
```

**5. Look at the end of the output.** It repeats the check on the server. The line for that platform
must say `[ok]`. Done.

## What to do for which problem
| What you see | What to do |
| --- | --- |
| Facebook or Instagram: expired token, "Session has expired", error 190 | Generate a new user token ([meta.md](meta.md)), paste it into `meta.facebook.access_token`, run `make meta-page-token`, then steps 3-4 |
| Threads token problem | Generate a new Threads token ([meta.md](meta.md)), paste it into `meta.threads.access_token`, run `uv run --env-file .env dk meta refresh --force`, then steps 3-4. It normally renews itself |
| YouTube: login stopped working | `make connect-youtube` (browser sign-in), then steps 3-4 |
| Telegram: bot token changed | Paste the new token into `telegram.bot_token`, run `make telegram-chat` and `make telegram-test`, then steps 3-4 |
| Google: "no access to the Sheet or folder" | Share the Sheet and the Drive folder with the service-account email again ([google.md](google.md)); no deploy needed |
| A post shows `failed` in the Sheet | Read `last_error` in that row. Fix what it says, then edit the row (change the time slightly) to try again |
| Nothing publishes at all | Look at the Dagster dashboard: `ssh -L 3000:localhost:3000 root@SERVER`, open <http://localhost:3000>, check Runs for red entries |

## Other changes and how to send them
| You changed | Command |
| --- | --- |
| A token, key, or any value in `dk.json` | `make deploy-secrets` |
| Which platforms are live (`deploy/platforms.production.yaml`) | `make deploy` |
| Code (after a new version of this project) | `make deploy` |
| Posts, captions, times | Only the Google Sheet. No deploy |

If you get stuck, send me the exact message from the terminal or the Telegram alert (never the token
itself) and say what you were trying to do.
