"""How a live post reaches each platform, and the quotas it applies: the facts in the publishing
contract that no adapter constant carries. Sources: docs/architecture.md (Platform adapters) and the
setup guides in docs/setup/. The pinned API version is added from config/platforms.yaml at start-up.
"""

from __future__ import annotations

from dk_publishing.domain.spec import PlatformFacts

FACTS: dict[str, PlatformFacts] = {
    "facebook": PlatformFacts("Meta Graph API: posts, photos and videos on the Page."),
    "instagram": PlatformFacts(
        "Instagram Graph API content publishing: Instagram fetches the file from a public link "
        "this server creates.",
        ("Instagram's rolling 24-hour API publishing limit, checked before every publish.",),
    ),
    "threads": PlatformFacts(
        "Threads API: Threads fetches the file from a public link this server creates.",
        ("250 posts per 24 hours.",),
    ),
    "youtube": PlatformFacts(
        "YouTube Data API v3 upload.",
        ("About 6 uploads a day (each upload uses about 1,600 of the daily API quota).",),
    ),
    "tiktok": PlatformFacts("TikTok Content Posting API."),
    "linkedin": PlatformFacts(
        "Handed to you on Telegram: caption ready to copy plus the images or video, and you post "
        "it in LinkedIn. (LinkedIn's Community Management API is not used.)"
    ),
}
