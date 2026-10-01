# ADR 0016: Facebook adapter publishes directly; credentials come from a token file for now

Status: accepted (2026-10-01)

## Decision

- The Facebook adapter (`adapters/platforms/facebook.py`) publishes **directly at the slot**: this system
  makes the post, it does not hand a scheduled post to Facebook. Formats: text post, one photo, one video
  (a multipart `source` upload to `graph-video.facebook.com`, up to 1 GiB in one request). Reels are refused
  with a clear message until the Reels upload flow is built.
- Native scheduling (Facebook holding the post) and the per-row `delivery` choice (`auto` / `direct` /
  `native`) are the next slice. It needs a schedule-native use case and, critically, cancelling the post at
  Facebook whenever a person withdraws, edits or deletes a natively scheduled row.
- The Page access token is read from a file named by `FACEBOOK_PAGE_TOKEN_FILE`, on every call, so rotating
  it needs no restart. The encrypted token vault (ADR 0010) replaces this when the connect flow exists.
- The Graph API version is pinned in `config/platforms.yaml` (`api_version`, currently v25.0, supported
  until July 2028; v26.0 was current on 2026-10-01). Bump it as a deliberate, tested change.

## Why

Meta gives no idempotency key, so the adapter's most important property is how it classifies failures:
a write whose answer is lost or that gets a 5xx is `UnknownOutcome` (reconciled by reading the Page's feed
back), never `Retryable`. Direct publishing keeps one code path while that is proven on a real Page.

## Consequences

- Matching a post to a variant after an uncertain outcome is by caption and creation time (within 15
  minutes before the slot). Two posts with an identical caption in that window cannot be told apart.
- A large video adds to the lateness because it is uploaded at the slot, not ahead of time.
- The `account` chosen in the Sheet is not yet cross-checked against `FACEBOOK_PAGE_ID`: one Page per
  deployment until the vault carries per-account tokens.
