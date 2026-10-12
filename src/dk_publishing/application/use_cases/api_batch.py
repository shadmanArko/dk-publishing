"""Many posts in one call: what an orchestrator uses to hand over a week of content at once.

Each post is handled exactly like a single `POST /v1/posts` (same checks, same idempotency on
post_key, same Telegram note), independently: one bad post never stops the others, and sending the
whole batch again is safe. A batch only ever creates drafts, whatever the key may do; a person
approves them in the Calendar. A media entry may be an uploaded media_id or a public https link,
which is downloaded once per batch and then used like an upload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from dk_publishing.application.ports import IntakeRejected
from dk_publishing.application.services import ApiServices
from dk_publishing.application.use_cases.api_media import fetch_media
from dk_publishing.application.use_cases.api_posts import (
    ApiError,
    PostRequest,
    PostView,
    Problem,
    submit_post,
    validate_post,
)

MAX_BATCH = 20
LINK = "https://"


@dataclass(frozen=True, slots=True)
class BatchItem:
    post_key: str
    ok: bool
    replayed: bool = False
    post: PostView | None = None
    error: str | None = None  # the refusal's kind: invalid, conflict, ...
    message: str | None = None
    problems: tuple[Problem, ...] = ()
    examples: Mapping[str, Mapping[str, Any]] | None = None


def submit_batch(
    api: ApiServices, requests: Sequence[PostRequest], *, actor_name: str, dry_run: bool
) -> list[BatchItem]:
    """Check (dry_run) or create every post; one answer per post, in the order sent."""
    if not requests:
        raise ApiError("invalid", "Send at least one post.")
    if len(requests) > MAX_BATCH:
        raise ApiError("invalid", f"At most {MAX_BATCH} posts per batch; send the rest in another.")
    fetched: dict[str, str] = {}  # link -> media_id, so a file shared by posts is fetched once
    results: list[BatchItem] = []
    for request in requests:
        try:
            request = replace(_fetch_links(api, request, actor_name, fetched), approve=False)
            if dry_run:
                checked = validate_post(api, request)
                results.append(
                    BatchItem(
                        request.post_key,
                        not checked.problems,
                        problems=tuple(checked.problems),
                        examples=checked.examples or None,
                    )
                )
            else:
                done = submit_post(api, request, actor_name=actor_name, may_approve=False)
                results.append(BatchItem(request.post_key, True, done.replayed, done.post))
        except ApiError as exc:
            results.append(
                BatchItem(
                    request.post_key,
                    False,
                    error=exc.kind,
                    message=exc.message,
                    problems=exc.problems,
                    examples=exc.examples or None,
                )
            )
    return results


def _fetch_links(
    api: ApiServices, request: PostRequest, actor_name: str, fetched: dict[str, str]
) -> PostRequest:
    variants = []
    for index, variant in enumerate(request.variants):
        media = []
        for position, item in enumerate(variant.media):
            if not item.startswith(LINK):
                media.append(item)
                continue
            if item not in fetched:
                try:
                    fetched[item] = fetch_media(api, item, None, created_by=actor_name).id
                except IntakeRejected as exc:
                    path = f"variants[{index}].media[{position}]"
                    raise ApiError(
                        "invalid", "A linked file could not be used; nothing was created.",
                        [Problem(path, exc.code, exc.message)],
                    ) from None  # fmt: skip
            media.append(fetched[item])
        variants.append(replace(variant, media=tuple(media)))
    return replace(request, variants=tuple(variants))
