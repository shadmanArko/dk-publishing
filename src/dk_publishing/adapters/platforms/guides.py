"""The machine-readable rules per platform, served by the API.

Limits are imported from the adapters, so there is one number per rule. `tests/unit/test_guides.py`
runs every example through the adapter's real `validate`, so a guide that disagrees with the
adapter fails the build.
"""

from __future__ import annotations

from dk_publishing.adapters.platforms import facebook, instagram, threads, tiktok, youtube
from dk_publishing.domain.guide import FieldGuide, FormatGuide, MediaGuide, PlatformGuide

MEDIA = "MEDIA_ID"  # stands for an id returned by POST /v1/media

DELIVERY = FieldGuide(
    "delivery",
    "choice",
    choices=("direct", "native"),
    note="direct (default): this service publishes at the slot. native: the platform holds the "
    "post and publishes it itself; the slot must be at least 10 minutes away.",
)


def _text(name: str, limit: int, *, required: bool = False, note: str = "") -> FieldGuide:
    return FieldGuide(name, "text", required=required, max_length=limit, note=note)


INSTAGRAM = PlatformGuide(
    "instagram",
    "Instagram feed photos, carousels, reels and stories.",
    (
        FormatGuide(
            "feed",
            "One photo in the feed.",
            MediaGuide(1, 1, instagram.PHOTO_TYPES, "JPEG only."),
            _text(
                "caption",
                instagram.MAX_CAPTION,
                note=f"At most {instagram.MAX_HASHTAGS} #hashtags.",
            ),
            (),
            {"format": "feed", "caption": "Fresh out of the pot.", "media": [MEDIA]},
        ),
        FormatGuide(
            "carousel",
            "Two to ten photos the viewer swipes through.",
            MediaGuide(
                *instagram.CAROUSEL_SIZE, instagram.PHOTO_TYPES, "JPEG only; order = swipe order."
            ),
            _text(
                "caption",
                instagram.MAX_CAPTION,
                note=f"At most {instagram.MAX_HASHTAGS} #hashtags.",
            ),
            (),
            {"format": "carousel", "caption": "Three plates.", "media": [MEDIA, MEDIA]},
        ),
        FormatGuide(
            "reel",
            "One short vertical video.",
            MediaGuide(1, 1, instagram.REEL_TYPES, "Vertical 9:16 works best."),
            _text(
                "caption",
                instagram.MAX_CAPTION,
                note=f"At most {instagram.MAX_HASHTAGS} #hashtags.",
            ),
            (
                FieldGuide("share_to_feed", "bool", note="Also show the reel on the profile grid."),
                FieldGuide("cover_at_s", "number", note="Second of the video to use as the cover."),
            ),
            {"format": "reel", "caption": "Slow-cooked.", "media": [MEDIA], "share_to_feed": True},
        ),
        FormatGuide(
            "story",
            "One photo or short video that disappears after 24 hours.",
            MediaGuide(1, 1, instagram.STORY_TYPES),
            None,
            (),
            {"format": "story", "media": [MEDIA]},
        ),
    ),
    ("Instagram fetches photos, carousels and stories from a public link the server creates.",),
)

FACEBOOK = PlatformGuide(
    "facebook",
    "Facebook Page posts: text, photos (one or an album) and video.",
    (
        FormatGuide(
            "post",
            "A text post, optionally with a link.",
            MediaGuide(0, 0),
            _text("caption", facebook.MAX_CAPTION, required=True),
            (FieldGuide("link", "text", note="A link attached to the post."), DELIVERY),
            {"format": "post", "caption": "Open today from noon."},
        ),
        FormatGuide(
            "photo",
            "One photo, or several as one album.",
            MediaGuide(
                1,
                facebook.MAX_ALBUM,
                facebook.PHOTO_TYPES,
                f"At most {facebook.MAX_PHOTO_BYTES // 1024**2} MB each.",
                facebook.MAX_PHOTO_BYTES,
            ),
            _text("caption", facebook.MAX_CAPTION),
            (),
            {"format": "photo", "caption": "Today's platter.", "media": [MEDIA]},
        ),
        FormatGuide(
            "video",
            "One video.",
            MediaGuide(
                1,
                1,
                facebook.VIDEO_TYPES,
                f"At most {facebook.MAX_VIDEO_BYTES // 1024**3} GB.",
                facebook.MAX_VIDEO_BYTES,
            ),
            _text("caption", facebook.MAX_CAPTION),
            (DELIVERY,),
            {"format": "video", "caption": "Watch the dum opening.", "media": [MEDIA]},
        ),
    ),
    ("Facebook reels are not supported; use video.",),
)

THREADS = PlatformGuide(
    "threads",
    "Threads posts: text, one image or one video.",
    tuple(
        FormatGuide(
            name,
            summary,
            media,
            _text("caption", threads.MAX_TEXT, required=name == "text"),
            (
                FieldGuide(
                    "reply_control", "choice", choices=threads.REPLY_CONTROLS, note="Who may reply."
                ),
            ),
            example,
        )
        for name, summary, media, example in (
            (
                "text",
                "Text only.",
                MediaGuide(0, 0),
                {"format": "text", "caption": "Biryani o'clock."},
            ),
            (
                "image",
                "One image.",
                MediaGuide(1, 1, threads.IMAGE_TYPES),
                {"format": "image", "caption": "Look at this.", "media": [MEDIA]},
            ),
            (
                "video",
                "One video.",
                MediaGuide(1, 1, threads.VIDEO_TYPES),
                {"format": "video", "caption": "Watch.", "media": [MEDIA]},
            ),
        )
    ),
    ("Threads counts characters its own way (links count as 23); the limit is 500.",),
)

YOUTUBE = PlatformGuide(
    "youtube",
    "YouTube video (a short vertical video under 60 seconds becomes a Short).",
    (
        FormatGuide(
            "video",
            "One video. Needs a title and an explicit made_for_kids answer.",
            MediaGuide(1, 1, youtube.VIDEO_TYPES),
            None,
            (
                _text("title", youtube.MAX_TITLE, required=True),
                _text("description", youtube.MAX_DESCRIPTION_BYTES, note="Limit is in bytes."),
                FieldGuide("tags", "text", note="Comma-separated; 500 characters together."),
                FieldGuide("category", "choice", choices=tuple(youtube.CATEGORIES)),
                FieldGuide(
                    "visibility_after_publish",
                    "choice",
                    choices=youtube.VISIBILITIES,
                    note="Default public.",
                ),
                FieldGuide("made_for_kids", "choice", required=True, choices=("yes", "no")),
                DELIVERY,
            ),
            {
                "format": "video",
                "media": [MEDIA],
                "title": "Dum biryani in 40 seconds",
                "description": "How we open the pot.",
                "made_for_kids": "no",
            },
        ),
    ),
    ("There is no caption field: use title and description.",),
)

TIKTOK = PlatformGuide(
    "tiktok",
    "TikTok video.",
    (
        FormatGuide(
            "video",
            "One video. You choose who can see it on every post; there is no default.",
            MediaGuide(1, 1, tiktok.VIDEO_TYPES),
            _text("caption", tiktok.MAX_CAPTION),
            (
                FieldGuide("privacy_level", "choice", required=True, choices=tuple(tiktok.PRIVACY)),
                FieldGuide("allow_comments", "bool"),
                FieldGuide("allow_duet", "bool"),
                FieldGuide("allow_stitch", "bool"),
                FieldGuide(
                    "commercial_disclosure",
                    "bool",
                    note="True if the post promotes your own business.",
                ),
            ),
            {"format": "video", "caption": "Biryani.", "media": [MEDIA], "privacy_level": "public"},
        ),
    ),
    ("Depending on how TikTok is set up here, the post may be handed to a person to publish.",),
)

GUIDES: dict[str, PlatformGuide] = {
    g.platform: g for g in (INSTAGRAM, FACEBOOK, THREADS, YOUTUBE, TIKTOK)
}
