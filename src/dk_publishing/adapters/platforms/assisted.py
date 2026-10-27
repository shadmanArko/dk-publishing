"""Assisted publishing: for a platform whose API we may not (yet) post with, the system hands the
post to a person instead of publishing it.

At the slot a Telegram card arrives with the caption ready to copy and the video (or images)
attached; the person posts it in the platform's own app. The variant then counts as "sent to you",
never as "published": nothing here can know whether the person went on to post it.

A card is sent at most once per variant. The record of that lives outside this class (`SentLog`), so
a run that dies between sending and recording is reconciled by asking the log, not by sending again.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime
from html import escape
from typing import Protocol

from dk_publishing.adapters.media.public import PublicMedia
from dk_publishing.application.ports import Handle, Notifier, NotifyError
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.errors import Rejected, Retryable
from dk_publishing.domain.publishing import (
    ASSISTED_PREFIX,
    LivePost,
    Rendition,
    VariantSnapshot,
    Violation,
)
from dk_publishing.domain.timezones import utc_to_berlin

# Telegram bots can send files up to 50 MB (the notifier's own limit is the same); a larger video is
# offered as a temporary download link instead of being attached.
ATTACH_LIMIT = 49 * 1024 * 1024
LINK_HOURS = 24  # how long such a link stays valid (the sweeper removes older ones)
PHOTO_LIMIT = 10 * 1024 * 1024  # Telegram bots may send photos up to 10 MB
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".m4v")


class SentLog(Protocol):
    def was_sent(self, key: str) -> bool: ...

    def mark_sent(self, key: str) -> None: ...


class AssistedRules(Protocol):
    """What differs per platform: the label, what a row must contain, and the settings to show."""

    label: str
    # A platform that also takes text-only posts sets `media_optional = True` (read with getattr).

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]: ...

    def settings(self, snapshot: VariantSnapshot) -> list[str]:
        """Human-readable lines such as 'Privacy: public', shown on the card."""


class AssistedPublisher:
    def __init__(
        self,
        *,
        rules: AssistedRules,
        capabilities: Capabilities,
        notifier: Notifier,
        sent: SentLog,
        public: PublicMedia | None = None,
    ) -> None:
        self._public = public
        self._rules = rules
        self._capabilities = capabilities
        self._notifier = notifier
        self._sent = sent

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]:
        return self._rules.validate(snapshot)

    def prepare(self, snapshot: VariantSnapshot, media: Sequence[Rendition]) -> Handle:
        """Build the card now (nothing is sent), so `publish` needs no snapshot."""
        if not media and not getattr(self._rules, "media_optional", False):
            raise Rejected("the media file was not downloaded, so there is nothing to hand over")
        paths = [m.path for m in media]
        for path in paths:
            if not os.path.isfile(path):
                raise Rejected(f"{os.path.basename(path)} is missing, so it cannot be sent")
        photos = bool(paths) and not paths[0].lower().endswith(VIDEO_SUFFIXES)
        for path in paths if photos else []:
            if os.path.getsize(path) > PHOTO_LIMIT:
                raise Rejected(
                    f"{os.path.basename(path)} is over Telegram's 10 MB photo limit, "
                    "so it cannot be handed over"
                )
        size = os.path.getsize(paths[0]) if paths else 0
        link = (
            self._public.expose(media[0])
            if self._public and media and not photos and size > ATTACH_LIMIT
            else None
        )
        names = snapshot.content.get("media") or [{}]
        name = str(names[0].get("name") or (os.path.basename(paths[0]) if paths else ""))
        return {
            "kind": "assisted",
            "tenant_id": snapshot.tenant_id,
            "variant_id": snapshot.variant_id,
            "file": paths[0] if paths else None,
            "files": paths,
            "name": name,
            "link": link,
            "card": self._card(snapshot, name, size, link, len(paths) if photos else 0),
        }

    def publish(self, handle: Handle) -> LivePost:
        card, path = handle.get("card"), handle.get("file")
        variant = handle.get("variant_id")
        files = [str(f) for f in handle.get("files") or ([path] if path else [])]
        optional = getattr(self._rules, "media_optional", False)
        if not (card and variant) or (not files and not optional):
            raise Rejected("this prepared post is missing what it needs to be handed over")
        key = _key(str(handle.get("tenant_id") or ""), str(variant))
        try:
            self._notifier.send(str(card))
            if files and not files[0].lower().endswith(VIDEO_SUFFIXES):
                self._notifier.send_photos(files, f"🖼 {handle.get('name') or 'images'}")
            elif files and os.path.getsize(files[0]) <= ATTACH_LIMIT:
                self._notifier.send_video(files[0], f"🎬 {handle.get('name', 'video')}")
        except NotifyError as exc:
            # Nothing can have gone wrong for the platform; at worst a duplicate card arrives.
            raise Retryable(f"could not send the card on Telegram: {exc}") from None
        self._sent.mark_sent(key)
        return LivePost(f"{ASSISTED_PREFIX}{variant}", None)

    def find_live(self, snapshot: VariantSnapshot, handle: Handle | None) -> LivePost | None:
        if self._sent.was_sent(_key(snapshot.tenant_id, snapshot.variant_id)):
            return LivePost(f"{ASSISTED_PREFIX}{snapshot.variant_id}", None)
        return None

    # --- the card --------------------------------------------------------------------------

    def _card(
        self, snapshot: VariantSnapshot, name: str, size: int, link: str | None, photos: int = 0
    ) -> str:
        label = escape(self._rules.label)
        slot = _local(snapshot.publish_at)
        caption = str(snapshot.content.get("caption") or "").strip()
        lines = [f"<b>📱 Post this on {label} now</b> (slot {slot})"]
        if caption:
            lines += ["", "<b>Caption</b> (tap to copy):", f"<code>{escape(caption)}</code>"]
        settings = self._rules.settings(snapshot)
        if settings:
            lines += ["", *[escape(s) for s in settings]]
        if photos:
            lines += ["", f"The {photos} image(s) follow in the next message, in posting order."]
        elif not name:
            pass  # a text-only post: nothing to attach
        elif size <= ATTACH_LIMIT:
            lines += ["", f"The video <i>{escape(name)}</i> follows in the next message."]
        elif link:
            lines += [
                "",
                f"<i>{escape(name)}</i> is too big for Telegram, so download it here "
                f"(link valid {LINK_HOURS} hours):\n{escape(link)}",
            ]
        else:
            lines += [
                "",
                f"⚠️ <i>{escape(name)}</i> is over Telegram's 50 MB limit, so it is not attached. "
                "Take it from your Drive media folder.",
            ]
        if photos:
            lines.append(f"Open {label}, start a post, add the images in order, paste the caption.")
        elif not name:
            lines.append(f"Open {label}, start a post and paste the caption.")
        else:
            lines.append(
                f"Open {label}, upload the video, paste the caption, choose the settings above."
            )
        return "\n".join(lines)


def _key(tenant_id: str, variant_id: str) -> str:
    return f"assist:{tenant_id}:{variant_id}"


def _local(moment: datetime) -> str:
    return f"{utc_to_berlin(moment):%d.%m. %H:%M}"
