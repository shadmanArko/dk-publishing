# How to schedule a post with the Google Sheet and Drive

For: anyone on the team who wants to publish. No technical knowledge needed.
Time: about 3 minutes per post.

## The idea in one picture

```
  Your computer / phone                Google                        The server                Social media
  ─────────────────────         ───────────────────────        ───────────────────        ────────────────────
  1. Put the video/photo   ───►  Drive folder                          │
     in the Drive folder         "DK Publishing Media"                 │
                                                                       │
  2. Write the post        ───►  Google Sheet "DK Publishing"   ──►  checks everything  ──►  Facebook, Instagram,
     (what, where, when)         tabs: Posts + one per platform        at the time you         Threads, YouTube,
                                                                       chose                   TikTok (via Telegram)
  3. Tick "ready"          ───►  (this approves the post)
```

You only touch the **Drive folder** and the **Sheet**. The server does the rest, at the minute you chose.
It never posts the same thing twice.

## Before you start (one time)

- You can open the Google Sheet **DK Publishing** and the Drive folder **DK Publishing Media**.
- Grey columns in the Sheet are filled in by the system. **Never type in grey columns.**
- **Never rename a column header.** (You may move columns around, and add your own columns on the far right.)

---

## Step 1: Put the file in Drive

1. Open the Drive folder **DK Publishing Media**.
2. Drag your video or photo in. Wait until the upload finishes.
3. Remember the **exact file name**, for example `eid_platter.mp4`.

| Do | Don't |
| --- | --- |
| Use simple names: `eid_platter.mp4`, `04_01.jpg` | Two files with the same name in the folder (the Sheet will refuse it) |
| Keep the name exactly the same in the Sheet (capital letters and `.mp4` count) | Rename the file after you approved the post |
| One file per name, replace nothing in place | Replace a file while a post using it is waiting (the post is withdrawn and has to be approved again) |

**Which files each platform takes**

| Platform | Format in the Sheet | Files |
| --- | --- | --- |
| Instagram | `feed` | 1 photo, **JPEG only** |
| Instagram | `carousel` | 2 to 10 JPEG photos (the order you write them is the swipe order) |
| Instagram | `reel` | 1 video (`.mp4`, `.mov`), vertical 9:16 works best |
| Instagram | `story` | 1 photo or video, **no caption** |
| Facebook | `post` | no file, text only |
| Facebook | `photo` | 1 to 10 photos (`.jpg`, `.png`), several = one album |
| Facebook | `video` | 1 video, up to 1 GB |
| Threads | `text` / `image` / `video` | none / 1 photo / 1 video |
| YouTube | `video` | 1 video |
| TikTok | (video) | 1 video |

The dropdowns also list Facebook `reel` and Threads `carousel`. They are **not supported yet**: pick Facebook `video` or Threads `image`.

## Step 2: Write the post on the **Posts** tab

One row = one idea. Fill in these cells:

| Column | What to write | Example |
| --- | --- | --- |
| `post_key` | A **unique** name. Never reused, never changed later. | `DK-2610-80` |
| `title` | Only for you. Never published. | `Eid platter reel` |
| `media` | File name(s) from Drive, separated by commas | `eid_platter.mp4` |
| `default_caption` | The text used by every platform that has an empty caption | `Slow-cooked for Eid. Order for Saturday! 🍛` |
| `default_slot` | When to publish, **Berlin time**, as a real date-time | `14.11.2026 18:00` |
| `ready` | **Leave it unticked for now** | ☐ |

Carousel example for `media`: `04_01.jpg, 04_02.jpg, 04_03.jpg`.

## Step 3: Say where it goes: one row per platform

Open the tab of each platform you want (**Facebook**, **Instagram**, **Threads**, **YouTube**, **TikTok**). Add one row per post. The columns on every platform tab:

| Column | What to do |
| --- | --- |
| `post_key` | Pick the same key from the dropdown |
| `enabled` | **Tick it.** Untick later to cancel |
| `account` | Pick the account from the dropdown |
| `caption` | Leave **empty** to use the default caption from Posts |
| `slot` | Leave **empty** to use the default slot. Fill it to post at a different time on this platform |

Then fill the platform's own columns:

**Instagram reel**
`format` = `reel`, `share_to_feed` = ☑ (also shows on your profile grid)

**Instagram carousel**
`format` = `carousel` (media needs 2 to 10 photos)

**Facebook video**
`format` = `video`

**YouTube** (all three are needed)
`title` = `Dum biryani in 40 seconds`, `made_for_kids` = `no`, optionally `description`, `tags`, `visibility_after_publish`

**TikTok**
`privacy_level` = `public` (you must choose it every time)

**Threads**
`format` = `image` (or `text` / `video`)

**Reddit** (not connected yet; the tab exists for later)
`subreddit`, `title`, `post_type` are required

> **Extra: `delivery`** (Facebook video, YouTube). Empty means `direct`: the server publishes at the minute you chose.
> `native` means Facebook/YouTube hold the post and publish it themselves (works even if our server is off). `native`
> needs the slot to be **at least 10 minutes ahead**.

### Worked example: one reel to three platforms at different times

**Posts tab**

| post_key | title | media | default_caption | default_slot | ready |
| --- | --- | --- | --- | --- | --- |
| DK-2610-80 | Eid platter reel | eid_platter.mp4 | Slow-cooked for Eid. Order for Saturday! 🍛 | 14.11.2026 18:00 | ☐ |

**Instagram tab**

| post_key | enabled | account | caption | slot | format | share_to_feed |
| --- | --- | --- | --- | --- | --- | --- |
| DK-2610-80 | ☑ | Dhaka Kacchi | *(empty)* | *(empty)* | reel | ☑ |

**YouTube tab**

| post_key | enabled | account | caption | slot | title | made_for_kids |
| --- | --- | --- | --- | --- | --- | --- |
| DK-2610-80 | ☑ | Dhaka Kacchi | *(empty)* | 14.11.2026 19:30 | Dum biryani in 40 seconds | no |

**TikTok tab**

| post_key | enabled | account | caption | slot | privacy_level |
| --- | --- | --- | --- | --- | --- |
| DK-2610-80 | ☑ | Dhaka Kacchi | *(empty)* | *(empty)* | public |

Result: Instagram and TikTok at 18:00, YouTube at 19:30, all with the same caption (YouTube uses the title instead of a caption).

## Step 4: Approve

Tick **`ready`** on the Posts row. That is your approval for every platform you enabled.

Within about **2 minutes** the `status` column on each platform row changes. Nothing is published until the slot.

## Step 5: Watch the status

| status | Meaning | What to do |
| --- | --- | --- |
| *(empty)* | Not approved yet (`ready` not ticked) | Tick `ready` when it is right |
| `draft` | Saved but not approved yet (for example `ready` is not ticked, or the row has a problem) | Check `last_error`, then tick `ready` |
| `invalid` | Something is wrong. **`last_error` says what, in plain words** | Fix it, the Sheet re-checks automatically |
| `approved` / `preparing` / `prepared` | Accepted and waiting for its time | Nothing |
| `scheduling_native` | Being handed to Facebook/YouTube to hold for you (a few seconds) | Nothing |
| `scheduled_native` | Facebook/YouTube is holding it for you | Nothing |
| `publishing` | Going out right now | Nothing |
| `published` | Live. **`live_url`** has the link | Done 🎉 |
| `sent to you` | TikTok: a card with the video was sent to you on **Telegram** | Post it by hand from Telegram |
| `unknown` | The server could not tell whether it went out and is checking with the platform | **Wait. Never create the post again** (that could post twice). It becomes `published` or `failed` |
| `failed` / `expired` | It did not go out. `last_error` says why | Fix and ask for help if unclear |
| `cancelled` | Cancelled by you | - |

The **Posts** tab shows a summary like `2 of 3 live, 1 failed`.

## Changing or cancelling a post

| I want to | Do this |
| --- | --- |
| Fix a typo **before** it is live | Edit the caption/slot in the row. It is withdrawn and approved again automatically (if `ready` is still ticked) |
| Cancel one platform | Untick `enabled` on that platform's row |
| Cancel everything | Untick `enabled` on every row of that post |
| Bring a cancelled post back | Tick `enabled` again (or put the row back) |
| Change a post that is already live | Not possible here. Edit it on the platform itself. Edits in the Sheet are ignored once it is live |

> **Safety brake.** If one sync would cancel **more than 5 scheduled posts at once** (for example because rows were deleted by
> accident), nothing is changed and you get a Telegram alert. Restore the rows or ask the developer to confirm.

## Posts that come from software (the Calendar tab)

Other programs or AI agents can also submit posts (see guide 02). They **never appear in the Posts or platform tabs**.
They wait for you in the **Calendar** tab:

1. Open **Calendar**. Every post of every platform is there, **sorted by date**.
2. The `source` column says `human` (typed here) or `agent` (from software), and `by` says which software.
3. Posts from software that wait for you show `awaiting_approval`, with the caption, the files and a `note`.
4. Scroll to the far right: tick **`approve`** to schedule it, or **`reject`** to cancel it.
5. Within about 2 minutes the row updates (an approved post gets its normal status, a rejected one disappears).

Only `awaiting_approval` rows react to ticks. Do not edit grey cells.

## The most common mistakes

| Mistake | What you see | Fix |
| --- | --- | --- |
| File name differs from Drive (`Eid_Platter.mp4` vs `eid_platter.mp4`) | `invalid`: "Media file ... was not found in the Drive folder" | Copy the exact name |
| Slot typed as plain text (`14/11 18h`) | `invalid`: slot must be a real date-time | Type `14.11.2026 18:00` so Google shows it as a date |
| Slot in the past | `invalid` | Pick a future time |
| Time on the clock-change night (the hour that does not exist or happens twice) | `invalid` | Pick another time |
| Forgot to tick `enabled` | Nothing happens for that platform | Tick it |
| Forgot `ready` | Everything stays empty | Tick `ready` on Posts |
| Two Posts rows with the same `post_key` | "appears more than once" | Keep one, give the other a new key |
| YouTube without `made_for_kids` or `title` | `invalid` | Fill both |
| TikTok without `privacy_level` | `invalid` | Choose one |
| Caption too long (Instagram 2,200 / Threads 500 characters) | `invalid`: shows the limit | Shorten it |
| Instagram feed with a PNG | `invalid`: JPEG only | Export as JPG |
| Instagram story with a caption | Nothing is wrong, but the caption is **not shown** | Stories carry no caption on Instagram; put the text in the image |
| Facebook `reel` or Threads `carousel` | `invalid`: "not supported yet" | Use Facebook `video`, or Threads `image` |
| Renamed a column header | Warning: "tab is missing column ..." and the tab is skipped | Restore the exact header name |

## Quick checklist (copy for every post)

- [ ] File is in the Drive folder and the name is spelled exactly.
- [ ] Posts row: unique `post_key`, `media`, `default_caption`, `default_slot` (a real date-time, in the future).
- [ ] One row per platform: `enabled` ☑, `account` chosen, platform columns filled.
- [ ] Ticked **`ready`**.
- [ ] After 2 minutes: no `invalid`. Check `last_error` if there is.
- [ ] At the slot: `published` and a `live_url` (TikTok: a Telegram card).
