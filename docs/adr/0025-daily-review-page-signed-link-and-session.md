# ADR 0025: A daily review page for the owner, opened from a signed link

Status: accepted

## Context

Agents now make drafts at volume: about 15 a day (3 ideas x 5 platforms), to be approved by the owner in under two
minutes, **watching each video**. ADR 0022 put approval in the Sheet's Calendar tab. That tab shows text only; the
videos and images exist only on the server (uploads) and on the agent's machine. Ticking 15 rows without seeing the
video is not a review.

## Decision

- **The page lives in this repository, in the existing API process** (`GET /review`, `POST /review/decide`), not in a
  new service and not in dk-intelligence. It needs the same database, the same media disk and the same approval
  code, and the public address, TLS and routing for `publish.` already exist. It is server-rendered HTML with no
  script and no outside file: forms for the buttons, a video player the browser provides. It is not part of
  `/openapi.json` (a page is not an API, and other software pins that document).
- **Every button ends in `review_drafts`**, the function the Calendar's tick boxes already use. It gained two
  keyword arguments (who decided, and where, for the history), nothing else: validation, freezing, the snapshot hash,
  the slot-in-the-past rule, cancelling and the webhooks that follow are unchanged and shared. There is no second
  approval path. The page decides exactly the variants it showed (the form carries their ids, at most 100), never
  "everything that is waiting now", so a draft that arrives while the owner is looking cannot be approved unseen.
  A variant that is no longer waiting is counted and left alone.
- **Only the owner can open it, by a signed link and a session cookie.**
  - The 08:00 Telegram digest gets a line "Review today's posts" with a link, only when agent drafts for today can
    still be approved, and only when the page is on.
  - The link is `payload.signature`: HMAC-SHA256 under a key derived from `review.secret`, with a purpose claim
    (`review-link`), an expiry (24 hours) and a random id. Opening it checks the signature first (constant time),
    then purpose and expiry, then sets a cookie and answers 303 to the clean address, so the token does not stay in
    the address bar or the history.
  - The cookie is a second signed token (purpose `review-session`, 12 hours): `HttpOnly`, `Secure`, `SameSite=Lax`,
    `Path=/review`. A link is refused as a cookie and a cookie as a link.
  - Every POST needs the cookie, a form token derived from that session (another session's token fails) and, when
    the browser sends `Origin`, the same host.
  - `dk review link`, run in the api container, prints a fresh link (base address from configuration, never in code).
- **Configuration is `dk.json`** (`review.secret`, `review.public_base_url`), like every other secret here, and
  `dk review init --base-url ...` on the Mac creates the secret (256 random bits) without printing it; `make
  deploy-secrets` ships it. Without a usable secret (32+ characters) and an https address the page is **off**: its
  routes are not registered (404, like any unknown address), the digest has no line and the Calendar has no `watch`
  links. A bad value turns it off and is reported by `make check-setup` as a warning, never a failure (a failure
  would page the owner nightly).
- **Media previews reuse the tokenised public links** (ADR 0009): the same unguessable token folder and read-only web
  server that Instagram and Threads fetch from, swept after 24 hours. The page asks for a link per file, keeps it
  for 12 hours and hands the same one out again on reload, so reloads do not pile up folders. No new file-serving
  route exists. Without the public-link settings the page names each file instead of playing it.
- **The Calendar's new `watch` column** holds, for agent posts, the page's address for that post's Berlin day with
  the idea as an anchor. It carries no credential (it is stored in a Sheet that others can see); opening it works if
  the owner signed in with the morning link in the last 12 hours, and otherwise explains in plain words how to get a
  link.

## Threat model

- **Someone finds the page's address:** it shows nothing and changes nothing without a valid session; a signed-out
  visit gets a plain page. The Sheet's `watch` links are therefore safe to share with whoever can see the Sheet.
- **Someone forges or alters a link or cookie:** needs the secret. Signature checked first, in constant time, before
  the payload is read; wrong purpose and expired tokens are refused. 10 forged attempts in 10 minutes closes the page
  for 10 minutes (per source address; behind the reverse proxy every visitor shares one address, which makes it a
  global limit, the safer way to fail). A forgery is counted; a missing or merely expired token is not.
- **The Telegram chat or a pasted link leaks:** a link is a bearer credential for up to 24 hours (and the session it
  makes lasts 12). It is not single-use on purpose: Telegram and browsers fetch links ahead of the owner, and a
  single-use link would be burned by the preview. Run `dk review init --rotate` to invalidate everything at once.
- **Cross-site request forgery:** `SameSite=Lax` cookie, per-session form token, `Origin` check.
- **A draft's text attacks the page:** every value from a draft is escaped; the response carries a content security
  policy with no script allowed at all, `no-store`, `no-referrer` and `DENY` framing headers.
- **Secrets in logs:** the token travels in the query string of one address; the access log filter cuts it off, and
  no code path logs a token, a cookie or the secret. A reverse proxy in front may log query strings: the link is only
  a login and ends in 24 hours.
- **A leaked preview address:** it is an unguessable folder name for a draft video, valid until the sweep (at most 24
  hours), like the links platforms already receive.
- **An agent key as an entry:** agent keys are not sessions and cannot reach the page; the page decides only
  `source = 'agent'` posts and records the owner as `human: review-page`. Agent keys stay draft-only; nothing here
  adds an approve right for software.

## Deliberately not done

- No accounts, passwords or second factor: one owner, one secret, short-lived links.
- No editing of captions on the page (cancel and resubmit, as everywhere, ADR 0022).
- No "reject everything for the day" button (too easy to hit on a phone); reject is per idea and per platform.
- No server-side request for a new link from the sign-in page (anyone could trigger Telegram messages).
- No database table for sessions (stateless, so no migration); revoking is rotating the secret.
- No streaming route for media: the existing public links are used. If the web server's public-link settings are
  missing, previews are missing, not worked around.

## Consequences

- One more thing in `dk.json` to carry to the server. Rotating the secret ends every link and session.
- The page's rendering is hand-written HTML (no template library added); the cost is a longer module, the gain is no
  new dependency.
- Sheet and page share one approval function, so a rule changed there (for example a limit) changes both.
