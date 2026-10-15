# ADR 0026: Sign in to the review page with the admin panel's email and password

Status: accepted

## Context

ADR 0025 opens the review page only from a signed 24 h link, kept as a 12 h session. In use that is awkward: the
digest has no link on a morning with nothing to approve, a link opened on a phone does not sign in the PC, and a second
person cannot be given access. The owner asked to sign in with the same email and password as the admin panel
(`dhakakacchi.com/admin`), on any device, without the sign-in expiring.

## Decision

- **One password, kept in one place.** This service never stores or copies a password. A sign-in form at
  `/review/login` forwards the email and password once, over https, to the admin backend's own `POST /v1/admin/login`
  and trusts only its answer (`adapters/auth/admin_login.py`). A 200 means yes; 400/401/403/422 mean no; anything else
  (down, slow, 5xx, not JSON) is "sign-in unavailable", never a yes. The admin session that check opens is closed again
  straight away (`/v1/admin/logout`), so nothing is left behind in the admin panel. Changing the admin password changes
  it here too.
- **A short list of who may come in.** `review.allowed_emails` in `dk.json`: being an admin does not by itself open the
  review page (it can approve public posts). An email not on the list is never even sent to the admin login.
- **A long session, not a forever one.** The cookie is the ADR 0025 signed session carrying the person's email, valid
  365 days and renewed on every visit (same session id, so an open form stays valid). A person who keeps using it never
  has to sign in again. A year is the longest a cookie is allowed to be treated as "no expiry" here.
- **Brakes on guessing.** The existing per-address failure limit, plus 8 wrong passwords per email in 15 minutes locks
  that email (even for the right password) for the rest of the window. One generic answer for every wrong or
  not-allowed attempt. A form from another site is refused (`Origin`).
- **Decisions name the person:** the history records `human: review:<email>` instead of `review-page`.
- **Sign out** (`/review/logout`, needs the form token) clears the cookie on that device.
- **The signed link stays** (digest, `dk review link`) as the recovery path and for anyone not on the list. If the
  settings are absent the page behaves exactly as before ADR 0025 left it.
- Configuration: `review.admin_login_url` (https, ending `/login`) and `review.allowed_emails` in `dk.json`, set with
  `dk review init --admin-login-url ... --allow-email ...` on the Mac; `make deploy-secrets` ships them.

## Threat model (what this adds)

- **A stolen cookie** works for up to a year, from that browser's holder. Revoking all of them is rotating the secret
  (`dk review init --rotate`); revoking one person is removing their email from `allowed_emails` (their cookie is then
  refused on the next request: the page checks the list on every visit). **Decision for the owner to revisit:** a
  changed admin password does not sign out devices already in.
- **The admin login is guessable from outside:** attackers can also call the admin backend directly, which has no
  limit of its own that we saw. This page adds limits on its own door only. A limit on the admin login itself is a
  recommended follow-up in the ordering backend (not changed here).
- **This service sees the password in transit** (it is typed into a form on this host). It is forwarded and dropped;
  it is not logged (form bodies are not) and not stored.
- **The admin backend is down:** nobody signs in with a password; the signed link still works.

## Deliberately not done

- No accounts, password reset or two-factor of our own (the admin panel owns them). Two-factor remains the next step if
  more people are added.
- No sharing of the admin panel's session cookie between the two sites (different hosts, different stacks).
- No change in the ordering repo.

## Consequences

- The review page now depends on the ordering backend being reachable for password sign-in; the link is the fallback.
- A third place to keep in step: the allowed list in `dk.json`.
