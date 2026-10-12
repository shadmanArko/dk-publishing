# Eval: the publishing contract, batches and MCP endpoint (2026-10-12)

`scripts/mcp_eval.py` runs on a throwaway database. It uses the real platform adapters (real rules) talking to fake
platforms, and Claude Sonnet through Claude Code, headless, connected over MCP with a `draft` key. Each job is
checked against the database or the API, not against the model's own words.

## Code path: plain HTTP, no AI
| Posts | Platforms | Checked (dry run) | Created | Resent | AI tokens | Time |
|---|---|---|---|---|---|---|
| 6 | 5 | 6 valid | 6 drafts | 6 replayed, 0 new | **0** | 0.48 s |

The batch takes 3 uploads and 3 batch calls. This is how an orchestrator hands over a week.

## AI path: an agent over MCP
| Job | Tools the agent called | Correct | Tokens (run 1 → run 2) |
|---|---|---|---|
| status: pipeline count and the plan's end | publishing_overview | yes | 6.5k → 6.6k |
| history: live links of last week's posts | list_posts | yes | 5.4k → 5.4k |
| rules: what a reel takes | publishing_spec | yes | 6.7k → 6.7k |
| submit a draft | publishing_spec ×2, submit_posts | yes | 19.2k → **14.4k** |
| fix a too-long post, then submit | publishing_spec, submit_posts | yes (shortened under 500) | 17.5k → **12.5k** |
| cancel a post | cancel_post | yes | 5.1k → 5.2k |
| **all six** | | **6 / 6** | **63k → 51k (−20%)** |

- **Run 1 → run 2:** the instructions no longer ask for a dry run before every submit. `submit_posts` creates only the
  posts that pass and returns what to fix for the rest, so the extra check was a wasted call.
- **Run 1's "status" was marked wrong by the eval, not the agent.** The agent answered 7 (6 from the code path + 1);
  the checker had expected 1. The checker now reads the truth from `GET /v1/overview`.
- **One call costs about 5k tokens**, mostly the tool descriptions and instructions the client sends each turn. A
  job that needs one tool is one call.

## Lessons
- Handing over content is a **code** step: 0 tokens. An agent is for questions and one-off drafts.
- Let errors teach instead of pre-checking: one call that fails with a valid example is cheaper than a check plus a
  submit.
- The property test found a real gap before release: a required caption of only spaces passed the schema but not
  the adapter. The schema now refuses it.
