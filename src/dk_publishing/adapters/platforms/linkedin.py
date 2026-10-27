"""LinkedIn, handed to a person for now (see assisted.py).

Posting to a company Page through LinkedIn's API needs the Community Management API, which LinkedIn
grants only after a review of the organisation and the app. Until then the post is sent to you on
Telegram: the caption ready to copy and the images or video attached. The same hand-over works for
posting from a personal profile, which usually reaches more people than a Page does.
"""

from __future__ import annotations

from dk_publishing.adapters.media.public import PublicMedia
from dk_publishing.adapters.platforms.assisted import AssistedPublisher, SentLog
from dk_publishing.application.ports import Notifier
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.publishing import VariantSnapshot, Violation

MAX_CAPTION = 3000
MAX_IMAGES = 9
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".gif")
VIDEO_TYPES = (".mp4", ".mov")
POST_AS = {
    "page": "the company Page",
    "profile": "your own profile",
}


class LinkedInRules:
    label = "LinkedIn"
    media_optional = True  # a text-only post is a normal LinkedIn post

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]:
        c = snapshot.content
        problems: list[Violation] = []
        caption = str(c.get("caption") or "")
        names = [str(m.get("name", "")) for m in c.get("media") or []]
        if len(caption) > MAX_CAPTION:
            problems.append(
                Violation(
                    "caption",
                    f"The caption is {len(caption)} characters; LinkedIn allows {MAX_CAPTION}.",
                )
            )
        if not caption.strip() and not names:
            problems.append(Violation("caption", "A text-only LinkedIn post needs a caption."))
        problems += _media_problems(names, str(c.get("format") or ""))
        post_as = str(c.get("post_as") or "")
        if post_as and post_as not in POST_AS:
            problems.append(Violation("post_as", f"Choose one of: {', '.join(POST_AS)}."))
        return problems

    def settings(self, snapshot: VariantSnapshot) -> list[str]:
        post_as = POST_AS.get(str(snapshot.content.get("post_as") or "page"), POST_AS["page"])
        return [f"Post as: {post_as}", "Put any link in the first comment, not in the post."]


def _media_problems(names: list[str], fmt: str) -> list[Violation]:
    """A row without a format (the Sheet) is judged by its files; a named format must match them."""
    video = any(n.lower().endswith(VIDEO_TYPES) for n in names)
    if fmt == "text" and names:
        return [Violation("media", "A text post has no media; use the image or video format.")]
    if fmt in ("image", "video") and not names:
        return [Violation("media", f"A LinkedIn {fmt} post needs its media.")]
    if (fmt == "video" and not video) or (fmt == "image" and video):
        return [Violation("media", f"The files are not a {fmt}; check the format.")]
    if not names:
        return []
    if video:
        if len(names) != 1:
            return [Violation("media", "A LinkedIn video post has exactly one video file.")]
        return []
    if len(names) > MAX_IMAGES:
        return [Violation("media", f"A LinkedIn post takes at most {MAX_IMAGES} images.")]
    bad = [n for n in names if not n.lower().endswith(IMAGE_TYPES)]
    if bad:
        return [
            Violation(
                "media",
                f"'{bad[0]}' is not an image ({', '.join(IMAGE_TYPES)}) or a video "
                f"({', '.join(VIDEO_TYPES)}).",
            )
        ]
    return []


def build_linkedin(
    capabilities: Capabilities,
    notifier: Notifier,
    sent: SentLog,
    public: PublicMedia | None = None,
) -> AssistedPublisher:
    return AssistedPublisher(
        rules=LinkedInRules(),
        capabilities=capabilities,
        notifier=notifier,
        sent=sent,
        public=public,
    )
