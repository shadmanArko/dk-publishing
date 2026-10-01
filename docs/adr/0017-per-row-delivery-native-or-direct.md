# ADR 0017: Per-row delivery: native (the platform publishes) or direct (we publish)

Status: accepted (2026-10-01)

## Decision

Platform tabs that support it get a `delivery` column: `direct` (blank means this) or `native`.

- **direct**: this system publishes at the slot (prepare ahead, publish at T). Unchanged.
- **native**: the post is handed to the platform right away as scheduled, and the platform publishes
  it itself at the slot, even if this system is off. Facebook: `published=false` plus
  `scheduled_publish_time` (10 minutes to 30 days ahead; text posts and videos).

`delivery` is an ordinary option, so it is part of the frozen content and of the row's `source_hash`:
changing it is an edit that withdraws the approval. The planner sees a platform's native window only
for a native row (`for_delivery`), so everything else behaves exactly as before.

## The safety rules this needed

1. **Withdraw before change.** Editing, unticking, deleting or switching off a row for a post the
   platform is holding deletes it at the platform first (`withdraw_native`). If that fails, the
   variant is left untouched and the sync reports the error; the next sync tries again.
2. **Never cancel a post at its slot.** From the slot on, the platform may already have published it,
   and deleting live posts is out of scope; the variant is left for the reconciliation to settle.
3. **An uncertain schedule is never repeated.** If the outcome of the schedule call is unknown, the
   platform may hold a copy this system cannot see; scheduling again could publish twice. The
   variant FAILS with a message telling a person to check the platform's scheduled posts. A run
   that dies mid-schedule is treated the same way after 10 minutes.
4. **Verify after the slot.** Ten minutes after the slot, `reconcile` asks the platform (by the
   scheduled post's id, not by caption) whether it is live. Live: published. Not live, or cannot
   tell: FAILED for a person (`scheduled_native -> failed` was added to the state machine).
5. **Too late to hand over** (scheduling delayed past the platform's minimum lead): fall back to
   direct publishing, as the plan says.

## Consequences

- Native needs the slot at least the platform's minimum lead away at approval time; the Sheet says so.
- A post withdrawn after the slot is not removed; fixing that is the (out of scope) live-edit feature.
- Instagram and Threads have no native scheduling API, so they only ever publish directly.
