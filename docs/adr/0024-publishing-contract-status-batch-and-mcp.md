# ADR 0024: One publishing contract, status for every post, batches, and an MCP endpoint

Status: accepted

## Context

More software is coming around publishing:
- a media agent that makes photos and videos;
- a writer agent for captions;
- an orchestrator that hands over a week of posts;
- assistants that answer "how far are we?".

Each needs to know **exactly** what the system accepts before it makes anything, to hand posts over cheaply, and to
see where publishing stands. ADR 0020 gave software the HTTP API, but four things were missing:
- The rules existed (`guides.py`, proven against the adapters), but without file sizes, timing, the route a post
  takes, a version or a schema.
- Reads covered only the API's own posts, so "how many are published, what is in the pipeline, when does the plan
  end" needed `psql` on the server.
- Each post took its own call.
- The MCP wrapper was listed as not built.

## Decision

- **One contract, generated from the checking code: `GET /v1/spec`.** It is built from:
  - the platform guides (which gained `media.max_bytes`);
  - `platforms.yaml` (mode, lead time, lateness, native window);
  - the connected accounts;
  - the API's request rules;
  - `adapters/platforms/facts.py`, for the two facts no adapter carries: the route a live post takes, and quotas.

  Each format has a valid example and a JSON Schema for offline checks; the API's check stays the authority.
  `spec_version` hashes the whole document and is the ETag (`If-None-Match` → 304). Every state has a meaning and
  an action for clients. `dk api spec` prints it.
- **File sizes are checked at submit**, from the uploaded size (`file_too_large`), instead of when the file is
  uploaded to the platform at the slot.
- **Status for every post, reading only.** The owner decided (2026-10-12) that a `read` key sees all posts:
  - `GET /v1/overview`: pipeline by state and platform, published, problems, next slots, the last planned slot;
  - `GET /v1/calendar`: filters; `state=published` is the history;
  - `GET /v1/posts/{post_key}` now shows Sheet posts too.

  Changing a Sheet post through the API stays refused.
- **`POST /v1/posts/batch`.** Up to 20 posts:
  - each is handled exactly like `POST /v1/posts`, independently and idempotently;
  - `dry_run` checks without creating;
  - **always drafts**, whatever the key may do;
  - a media entry may be a public https link, fetched once per batch through the same intake as `POST /v1/media`.
- **MCP at `/mcp/` on the same app, the same keys, the same use cases** (`entrypoints/publish_mcp`, FastMCP pinned,
  stateless JSON). The owner decided (2026-10-12) that it is public on the API domain, like the API itself. Six
  tools:
  - `read` keys: `publishing_overview`, `list_posts`, `get_post`, `publishing_spec`;
  - `draft` keys: `submit_posts`, `cancel_post`.

  There is **no approve tool, whatever the key**: a person approves in the Calendar. Tools are hidden from lists and
  refused on call by right. There is a per-key rate limit, a size cap on answers, and one log line per call (tool,
  key, status, ms, a hash of the arguments, never a token). It is opt-in with `mcp.enabled` in `dk.json`, and
  `mcp.disabled_tools` switches single tools off. MCP answers are the HTTP answers (tested). Files never travel
  through MCP.
- `dk api mcp-config --client ...` prints client settings. They name the key only as `DK_PUBLISH_KEY`.

## Consequences

- A producer reads the contract once (cached by version), makes content to it, and hands over a week in one call.
  Publishing itself costs a calling program nothing, and an AI only needs the slice for what it makes.
- The API's error codes, the guides and `llms.txt` stay in sync through `tests/unit/test_docs_in_sync.py`.
- A `read` key now sees the owner's Sheet posts (captions included). Give keys only to trusted software.
- MCP is public with the same keys as the API: no new power, but one more door to watch in the logs.
- Not built: production advice with sources (aspect ratio, resolution, duration). A media probe at upload would turn
  some of it into checked rules; that needs `ffprobe` in the image, the owner's call.
