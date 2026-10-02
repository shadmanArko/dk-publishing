"""Assisted publishing: for a platform whose API we may not (yet) post with, the system hands the
post to a person instead of publishing it.

At the slot a Telegram card arrives with the caption ready to copy and the video attached; the
person posts it in the platform's own app. The variant then counts as "sent to you", never as
"published": nothing here can know whether the person went on to post it.

A card is sent at most once per variant. The record of that lives outside this class (`SentLog`), so
a run that dies between sending and recording is reconciled by asking the log, not by sending again.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime
from html import escape
from typing import Protocol

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

# Telegram bots can send files up to 50 MB; a larger video is described instead of attached.
ATTACH_LIMIT = 45 * 1024 * 1024


class SentLog(Protocol):
    def was_sent(self, key: str) -> bool: ...

    def mark_sent(self, key: str) -> None: ...


class AssistedRules(Protocol):
    """What differs per platform: the label, what a row must contain, and the settings to show."""

    label: str

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
    ) -> None:
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
        if not media:
            raise Rejected("the media file was not downloaded, so there is nothing to hand over")
        path = media[0].path
        if not os.path.isfile(path):
            raise Rejected(f"{os.path.basename(path)} is missing, so it cannot be sent")
        return {
            "kind": "assisted",
            "tenant_id": snapshot.tenant_id,
            "variant_id": snapshot.variant_id,
            "file": path,
            "name": os.path.basename(path),
            "card": self._card(snapshot, os.path.basename(path), os.path.getsize(path)),
        }

    def publish(self, handle: Handle) -> LivePost:
        card, path = handle.get("card"), handle.get("file")
        variant = handle.get("variant_id")
        if not (card and path and variant):
            raise Rejected("this prepared post is missing what it needs to be handed over")
        key = _key(str(handle.get("tenant_id") or ""), str(variant))
        try:
            self._notifier.send(str(card))
            if os.path.getsize(str(path)) <= ATTACH_LIMIT:
                self._notifier.send_video(str(path), f"🎬 {handle.get('name', 'video')}")
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

    def _card(self, snapshot: VariantSnapshot, name: str, size: int) -> str:
        label = escape(self._rules.label)
        slot = _local(snapshot.publish_at)
        caption = str(snapshot.content.get("caption") or "").strip()
        lines = [f"<b>📱 Post this on {label} now</b> (slot {slot})"]
        if caption:
            lines += ["", "<b>Caption</b> (tap to copy):", f"<code>{escape(caption)}</code>"]
        settings = self._rules.settings(snapshot)
        if settings:
            lines += ["", *[escape(s) for s in settings]]
        if size <= ATTACH_LIMIT:
            lines += ["", f"The video <i>{escape(name)}</i> follows in the next message."]
        else:
            lines += [
                "",
                f"⚠️ <i>{escape(name)}</i> is over Telegram's 50 MB limit, so it is not attached. "
                "Take it from your Drive media folder.",
            ]
        lines.append(
            f"Open {label}, upload the video, paste the caption, choose the settings above."
        )
        return "\n".join(lines)


def _key(tenant_id: str, variant_id: str) -> str:
    return f"assist:{tenant_id}:{variant_id}"


def _local(moment: datetime) -> str:
    return f"{utc_to_berlin(moment):%d.%m. %H:%M}"
