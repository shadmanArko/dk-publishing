# Runbook: switch on the daily review page and get your first link

For the owner. ADR 0025 says why; guide 02 section 5g says how to use it. Until step 1 is done the page **does not
exist** (nothing else changes). The server's address never goes in this repository: you type it below.

Two machines are involved. **Mac** = your computer, in the dk-publishing folder. **Server** = over SSH.

> The secret lives in your local `dk.json`, like every other secret here, and `make deploy-secrets` sends it. A
> secret typed straight into the server's copy would be overwritten by the next `make deploy-secrets`. That is why
> the secret is made on your Mac and never typed or shown anywhere.

## 1. Mac: make the secret and set the address

The address is the API's public address, the same one agents use (`API_DOMAIN` in `deploy/server.conf`), starting
with `https://` and with no path.

```bash
cd "/Users/shadman.arko/Documents/Work_DoNotTouch/Dhaka Kacchi/dk-publishing"
uv run --env-file .env dk review init --base-url https://YOUR-API-ADDRESS
```

Expected: one line saying `[did] made a new secret and saved the review page address in .../dk.json (the secret is
not shown)`. Run again later and it keeps the secret (`kept the existing secret`); add `--rotate` only to cancel every
link and session already handed out.

Close any editor window that has `dk.json` open (an old window overwrites newer changes when saved).

```bash
make check-setup
```

Expected, among the lines: `[ok]   review page: on (the secret and the public address are set)`. A `[warn]` line
means the address is not `https://` or the secret is too short: run the first command again.

## 2. Mac: send it to the server

```bash
make deploy-secrets
```

This ships the code **and** `dk.json`, rebuilds and restarts the services. It does not post anything. Look at the end
of the output: the `check-setup` it runs on the server must also say `review page: on`.

Then add the new column to the live Sheet (adds the `watch` header, never clears data):

```bash
make sheet-init-dry
make sheet-init
```

Expected: the dry run lists `add headers to 'Calendar': watch`; the real run does it. The next Sheet sync (within a few
minutes) fills the column for agent rows.

## 3. Server: restart the API and print your first link

The API reads `dk.json` once when it starts, so restart it (harmless) and ask for a link. You were already told the
server's address when it was set up (`deploy/server.conf`); use your usual SSH command.

```bash
cd /srv/dk/app/deploy
docker compose -f compose.prod.yaml --env-file .env restart api
docker compose -f compose.prod.yaml --env-file .env exec api dk review link
```

Expected: one line, `https://YOUR-API-ADDRESS/review/enter?t=...&day=YYYY-MM-DD`, and under it (on screen only) `Valid for
24 hours`. The link is a login: do not paste it anywhere but your own phone or browser.

Another day: `dk review link --day 2026-10-20`.

## 4. Phone: check it

1. Open the link. You should land on `/review?day=...` with the address bar **not** showing `t=`.
2. With agent drafts for that day you see one block per idea, the video playing, and each platform below it.
3. A first safe try: **reject** a throwaway draft (the agent can send one). Approve only a post you really want out.
4. Tomorrow at 08:00 the Telegram digest has the line **Review today's posts** if drafts wait for that day.

## When something is off

| What you see | Cause and fix |
| --- | --- |
| The page answers "Not found" | The page is off: step 1 or 2 was skipped, or `check-setup` showed a `[warn]`. Redo them |
| "That link is not valid" for a fresh link | The secret changed after the link was made (a rotation), or the API was not restarted after `make deploy-secrets`. Restart `api` (step 3) and print a new link |
| "That link or sign-in has expired" | Links last 24 hours, a sign-in 12. Print or open a newer one |
| "Too many wrong attempts" | Wait ten minutes; nobody harmed anything, a forged link was tried 10 times |
| A video does not play ("No preview for ...") | The file was removed (files go a week after their post is done), or the server has no public-link settings: `media.public_dir`, `media.public_base_url` in `dk.json`, set by `make deploy-secrets` |
| No review line in the 08:00 Telegram message | Nothing was waiting for that day (or its slots had passed), or the page is off. `dk alerts digest` shows if today's was already sent |
| I think a link leaked | Mac: `uv run --env-file .env dk review init --base-url https://YOUR-API-ADDRESS --rotate`, then `make deploy-secrets`, then restart `api` (step 3). Every older link and sign-in stops working |

Switch the page off again: empty `review.secret` in `dk.json` (or remove the section), `make deploy-secrets`, restart
`api`. The digest line and the `watch` links stop with it.


## 5. Optional: sign in with your admin email and password (ADR 0026)

Lets you (and anyone you name) open the page from any device with the same email and password as the admin panel, and
stay signed in for a year. The link in steps 3 and 4 keeps working. On your **Mac**:

```bash
cd "/Users/shadman.arko/Documents/Work_DoNotTouch/Dhaka Kacchi/dk-publishing"
uv run --env-file .env dk review init --base-url https://YOUR-PUBLISH-ADDRESS \
  --admin-login-url https://YOUR-API-ADDRESS/v1/admin/login --allow-email you@example.com
make check-setup
make deploy-secrets
```

Repeat `--allow-email` for each person. Then open `https://YOUR-PUBLISH-ADDRESS/review/login` and sign in. Wrong
password, or an email that is not on the list, gives the same message on purpose. To remove someone, run the first
command again with only the emails that should stay (it replaces the list), then `make deploy-secrets`.
