"""TikTok, handed to a person for now (see assisted.py).

TikTok's API restricts unaudited apps to private posts and asks that the person confirms each post,
so until TikTok has audited this integration the post is sent to you on Telegram. The Sheet's TikTok
tab already collects the choices TikTok requires per post (privacy level, comments, duet, stitch,
promotion), and they are shown on the card.
"""

from __future__ import annotations

from dk_publishing.adapters.media.public import PublicMedia
from dk_publishing.adapters.platforms.assisted import AssistedPublisher, SentLog
from dk_publishing.application.ports import Notifier
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.publishing import VariantSnapshot, Violation

MAX_CAPTION = 2200  # conservative: TikTok has raised its limit before, never lowered it
VIDEO_TYPES = (".mp4", ".mov", ".webm")
PRIVACY = {
    "public": "Everyone",
    "friends": "Friends",
    "followers": "Followers",
    "only_me": "Only me",
}


class TikTokRules:
    label = "TikTok"

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]:
        c = snapshot.content
        problems: list[Violation] = []
        caption = str(c.get("caption") or "")
        if len(caption) > MAX_CAPTION:
            problems.append(
                Violation(
                    "caption",
                    f"The caption is {len(caption)} characters; keep it under {MAX_CAPTION}.",
                )
            )
        names = [str(m.get("name", "")) for m in c.get("media") or []]
        if len(names) != 1:
            problems.append(Violation("media", "A TikTok post needs exactly one video file."))
        elif not names[0].lower().endswith(VIDEO_TYPES):
            problems.append(
                Violation("media", f"'{names[0]}' is not a video ({', '.join(VIDEO_TYPES)}).")
            )
        if str(c.get("privacy_level") or "") not in PRIVACY:
            problems.append(
                Violation(
                    "privacy_level",
                    f"Choose who can see it: {', '.join(PRIVACY)}. TikTok has no default.",
                )
            )
        return problems

    def settings(self, snapshot: VariantSnapshot) -> list[str]:
        c = snapshot.content
        privacy = PRIVACY.get(str(c.get("privacy_level") or ""), "choose it yourself")
        return [
            f"Who can view: {privacy}",
            f"Comments: {_on(c.get('allow_comments'))} · Duet: {_on(c.get('allow_duet'))}"
            f" · Stitch: {_on(c.get('allow_stitch'))}",
            "Promotes your own business: "
            + (
                "yes, switch on the commercial-content label"
                if c.get("commercial_disclosure")
                else "no"
            ),
        ]


def _on(value: object) -> str:
    return "on" if value else "off"


def build_tiktok(
    capabilities: Capabilities,
    notifier: Notifier,
    sent: SentLog,
    public: PublicMedia | None = None,
) -> AssistedPublisher:
    return AssistedPublisher(
        rules=TikTokRules(), capabilities=capabilities, notifier=notifier, sent=sent, public=public
    )
