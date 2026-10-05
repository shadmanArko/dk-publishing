"""`dk live-test <platform>`: really publish one small post and read it back from the platform.

This is the proof that a set of credentials works end to end. It bypasses the Sheet and the
database on purpose, so it can be run before anything else is set up. It posts for real, so it
needs `--yes`; the post has to be deleted by hand afterwards (YouTube videos are private).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from dk_publishing.adapters.config.platforms import ConfigError, PlatformSettings
from dk_publishing.adapters.platforms.live import build_live_publisher
from dk_publishing.domain.errors import PublishingError
from dk_publishing.domain.publishing import Rendition, VariantSnapshot

SUPPORTED = ("facebook", "threads", "instagram", "youtube", "tiktok")
NEEDS_VIDEO = ("instagram", "youtube", "tiktok")


@dataclass
class LiveTestResult:
    ok: bool
    lines: list[str] = field(default_factory=list)


def _content(platform: str, text: str, video: Path | None) -> dict[str, Any]:
    media = [{"name": video.name}] if video else []
    if platform == "youtube":
        return {
            "title": text[:100],
            "description": "Created by the DK Publishing live test. Safe to delete.",
            "visibility_after_publish": "private",
            "made_for_kids": "no",
            "media": media,
        }
    if platform == "tiktok":
        # Always private: an unaudited TikTok app may only post "only me".
        return {"caption": text, "privacy_level": "only_me", "allow_comments": True, "media": media}
    if platform == "instagram":
        return {"format": "reel", "caption": text, "media": media}
    if platform == "threads":
        return {"format": "text", "caption": text}
    return {"format": "video" if video else "post", "caption": text, "media": media}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_live_test(
    platform: str,
    env: Mapping[str, str],
    settings: PlatformSettings,
    *,
    text: str | None = None,
    video: Path | None = None,
    confirmed: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> LiveTestResult:
    if platform not in SUPPORTED:
        raise ConfigError(f"no live test for {platform!r}; choose one of: {', '.join(SUPPORTED)}")
    if platform in NEEDS_VIDEO and video is None:
        raise ConfigError(f"{platform} needs a video file: add --video /path/to/clip.mp4")
    if video is not None and not video.is_file():
        raise ConfigError(f"no such video file: {video}")
    stamp = f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC"
    text = text or f"DK Publishing test post ({stamp}). Safe to delete."
    content = _content(platform, text, video)
    result = LiveTestResult(ok=False)
    what = (
        "a PRIVATE video"
        if platform in ("youtube", "tiktok")
        else f"a {content.get('format', 'video')}"
    )
    result.lines.append(f"{platform}: will publish {what}: {text[:80]!r}")
    if not confirmed:
        result.lines.append("Nothing was posted. Add --yes to really publish it.")
        return result

    try:
        publisher = build_live_publisher(platform, settings, env, transport)
        snapshot = VariantSnapshot(
            variant_id=str(uuid.uuid4()),
            tenant_id="live-test",
            platform=platform,
            account_id="live-test",
            publish_at=datetime.now(UTC),
            content=content,
        )
        problems = publisher.validate(snapshot)
        if problems:
            result.lines += [f"[FAIL] {p.field}: {p.message}" for p in problems]
            return result
        renditions = [Rendition("original", str(video), _sha256(video))] if video else []
        handle = publisher.prepare(snapshot, renditions)
        result.lines.append("[ok]   prepared")
        live = publisher.publish(handle)
        result.lines.append(f"[ok]   published: id {live.external_id}")
        confirmed_live = publisher.find_live(snapshot, handle)
    except PublishingError as exc:
        result.lines.append(f"[FAIL] {type(exc).__name__}: {exc}")
        return result
    if confirmed_live is None:
        result.lines.append("[FAIL] published, but reading it back from the platform found nothing")
        return result
    result.ok = True
    result.lines.append("[ok]   read back from the platform: it is there")
    if live.url or confirmed_live.url:
        result.lines.append(f"       open it: {confirmed_live.url or live.url}")
    result.lines.append("Delete the test post by hand when you have looked at it.")
    return result
