# Guides

Simple, step-by-step guides with examples. Start here.

| Guide | For | What you learn |
| --- | --- | --- |
| [01. Schedule a post with the Google Sheet and Drive](01-post-with-the-sheet.md) | Anyone on the team | Upload the file, write the post, choose platforms, approve, watch the status, fix mistakes. Also how to approve posts that software submitted (Calendar tab). |
| [02. The Publishing API](02-api-for-developers-and-agents.md) | A developer, or an AI agent | Keys, uploading, validating, creating, following and cancelling posts, ready-to-send examples for every platform, errors and how to fix them, code in Python and JavaScript. |

## How the pieces fit

```
 Google Drive folder  ─┐                                   ┌─►  Facebook
 (media files)         ├─►  Google Sheet  ─┐               ├─►  Instagram
                       │    (Posts + one   ├─►  Server  ───┼─►  Threads
 Other software or  ───┘     tab per       │   (checks,    ├─►  YouTube
 an AI agent (API) ───────►  platform)  ───┘   approval,   └─►  TikTok (sent to you on Telegram)
                             Calendar tab      schedule)
                             = everything, by date
```

- **Human posts** come from the Sheet and Drive. You approve by ticking `ready` on the Posts tab.
- **Software posts** come from the API and wait in the **Calendar** tab. You approve or reject them with the tick boxes.
- Both are checked the same way, published at the chosen minute, and never published twice.
- Telegram tells you about failures, drafts waiting for you, and TikTok posts to publish by hand.

More detail for developers of this system: `docs/architecture.md`, `docs/adr/`, `docs/runbooks/`.
