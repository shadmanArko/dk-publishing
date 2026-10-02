"""Build the real publisher for a platform that is switched to `live`.

Interim credentials: the Page id and a token file named in the environment. The encrypted token
vault replaces this when the connect flow exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import httpx

from dk_publishing.adapters.config.platforms import ConfigError, PlatformSettings
from dk_publishing.adapters.media.public import PublicMediaStore
from dk_publishing.adapters.platforms.facebook import FacebookPublisher
from dk_publishing.adapters.platforms.instagram import InstagramPublisher
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.adapters.platforms.meta_credentials import MetaCredentials, SectionTokenProvider
from dk_publishing.adapters.platforms.threads import DEFAULT_VERSION as THREADS_VERSION
from dk_publishing.adapters.platforms.threads import HOST as THREADS_HOST
from dk_publishing.adapters.platforms.threads import ThreadsPublisher
from dk_publishing.adapters.platforms.tokens import FileTokenProvider
from dk_publishing.adapters.platforms.youtube import YouTubePublisher
from dk_publishing.adapters.platforms.youtube_api import GoogleYouTubeApi, YouTubeCredentials
from dk_publishing.application.ports import Publisher
from dk_publishing.domain.capabilities import Capabilities

DEFAULT_GRAPH_VERSION = "v25.0"


def build_live_publisher(
    name: str,
    settings: PlatformSettings,
    env: Mapping[str, str],
    transport: httpx.BaseTransport | None = None,
) -> Publisher:
    caps = settings.capabilities  # each row's `delivery` choice decides whether the window is used
    if name == "facebook":
        one_file = env.get("META_CREDENTIALS_FILE", "").strip()
        if one_file:
            credentials = MetaCredentials(one_file)
            page_id = str(credentials.section("facebook").get("page_id") or "").strip()
            if not page_id:
                raise ConfigError(f"facebook.page_id is empty in {credentials.path}")
            graph = GraphClient(
                version=settings.api_version or DEFAULT_GRAPH_VERSION,
                tokens=SectionTokenProvider(credentials, "facebook"),
                transport=transport,
            )
            return FacebookPublisher(page_id=page_id, capabilities=caps, graph=graph)
        page_id = env.get("FACEBOOK_PAGE_ID", "").strip()
        token_file = env.get("FACEBOOK_PAGE_TOKEN_FILE", "").strip()
        missing = [
            var
            for var, value in (
                ("FACEBOOK_PAGE_ID", page_id),
                ("FACEBOOK_PAGE_TOKEN_FILE", token_file),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"facebook is live but {', '.join(missing)} is not set in .env "
                "(or set META_CREDENTIALS_FILE)"
            )
        graph = GraphClient(
            version=settings.api_version or DEFAULT_GRAPH_VERSION,
            tokens=FileTokenProvider(Path(token_file).expanduser()),
            transport=transport,
        )
        return FacebookPublisher(page_id=page_id, capabilities=caps, graph=graph)
    if name == "threads":
        return _threads(settings, caps, env, transport)
    if name == "instagram":
        return _instagram(settings, caps, env, transport)
    if name == "youtube":
        return _youtube(caps, env)
    raise ConfigError(f"platform {name!r} is live but no such adapter is built yet")


def _credentials(env: Mapping[str, str], platform: str) -> MetaCredentials:
    one_file = env.get("META_CREDENTIALS_FILE", "").strip()
    if not one_file:
        raise ConfigError(f"{platform} is live but META_CREDENTIALS_FILE is not set in .env")
    return MetaCredentials(one_file)


def _public_media(env: Mapping[str, str]) -> PublicMediaStore | None:
    """Public links for media, available only where a web server serves PUBLIC_MEDIA_DIR."""
    directory = env.get("PUBLIC_MEDIA_DIR", "").strip()
    base = env.get("PUBLIC_MEDIA_BASE_URL", "").strip()
    return PublicMediaStore(Path(directory).expanduser(), base) if directory and base else None


def _threads(
    settings: PlatformSettings,
    caps: Capabilities,
    env: Mapping[str, str],
    transport: httpx.BaseTransport | None,
) -> Publisher:
    credentials = _credentials(env, "threads")
    user_id = str(credentials.section("threads").get("user_id") or "").strip()
    if not user_id:
        raise ConfigError(f"threads.user_id is empty in {credentials.path}")
    graph = GraphClient(
        version=settings.api_version or THREADS_VERSION,
        tokens=SectionTokenProvider(credentials, "threads"),
        transport=transport,
        host=THREADS_HOST,
        video_host=THREADS_HOST,
    )
    return ThreadsPublisher(
        user_id=user_id, capabilities=caps, graph=graph, public=_public_media(env)
    )


def _instagram(
    settings: PlatformSettings,
    caps: Capabilities,
    env: Mapping[str, str],
    transport: httpx.BaseTransport | None,
) -> Publisher:
    credentials = _credentials(env, "instagram")
    account_id = str(credentials.section("instagram").get("account_id") or "").strip()
    if not account_id:
        raise ConfigError(f"instagram.account_id is empty in {credentials.path}")
    version = settings.api_version or DEFAULT_GRAPH_VERSION
    graph = GraphClient(
        version=version,
        # Facebook Login: the Instagram token may simply be the Page token.
        tokens=SectionTokenProvider(credentials, "instagram", fallback="facebook"),
        transport=transport,
    )
    return InstagramPublisher(
        account_id=account_id,
        capabilities=caps,
        graph=graph,
        version=version,
        public=_public_media(env),
    )


def _youtube(caps: Capabilities, env: Mapping[str, str]) -> Publisher:
    path = env.get("YOUTUBE_CREDENTIALS_FILE", "").strip()
    if not path:
        raise ConfigError("youtube is live but YOUTUBE_CREDENTIALS_FILE is not set in .env")
    credentials = YouTubeCredentials(path)
    credentials.load()  # fail at start-up if the file is missing or broken; tokens are read later
    return YouTubePublisher(api=GoogleYouTubeApi.from_credentials(credentials), capabilities=caps)
