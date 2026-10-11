# ADR 0022: Posts from the API are approved in the Sheet's Calendar tab

Status: accepted

## Context

Two kinds of post exist: people type rows in the Sheet (ADR 0003) and software submits them through the API
(ADR 0020). The owner wants one place to see every post by date, to tell human from agent, and to approve in the
Sheet as before. Approval had to be `dk api approve` or an `approve`-right key.

## Decision

- **Postgres stays the only record; the Sheet is a view with two controls.** No two-way sync of content, and the
  sync still never edits or cancels an API post's content. An agent's post is changed by cancelling and resubmitting.
- **The Calendar tab lists everything**, sorted by slot: human and agent posts, approved or later, plus agent posts
  still waiting (`awaiting_approval`) or refused (`invalid`). New system columns: `format`, `caption`, `media`, `by`
  (the API key), `note` (what needs the person, or why a tick was refused). `source` still says human or agent.
- **Two checkboxes on the Calendar, `approve` and `reject`.** They react only to agent posts that are waiting. A
  tick on anything else is stale and ignored; both ticked does nothing and says so. Approve goes through the same
  `approve` use case as every other approval (validated, frozen, hashed, actor `sheet`); a slot that has passed is
  refused like `dk api approve`. Reject cancels. Row matching is by `(post_key, platform, account)` columns, so
  sorting or filtering the Calendar is safe.
- **The tab is regenerated every sync**, so ticks are cleared by the rewrite. An unticked box reads back as `FALSE`;
  the comparison treats it as empty so a no-op sync still writes 0 cells (the loop trap in CLAUDE.md).
- **Old Sheets keep working.** The Calendar is not a required tab: without the new columns nothing is read or written
  for them. `dk sheet init` appends the columns on the right.

## Consequences

- A tick made in the few seconds between the sync reading the Sheet and rewriting the Calendar is lost; the person
  sees the box clear and the status unchanged, and ticks again. Nothing is applied twice (compare-and-set).
- The Calendar is now partly input. Do not add free-text editing there.
