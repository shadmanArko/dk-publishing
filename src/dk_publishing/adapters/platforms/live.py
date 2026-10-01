"""Build the real publisher for a platform that is switched to `live`.

Interim credentials: the Page id and a token file named in the environment. The encrypted token
vault replaces this when the connect flow exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import httpx

from dk_publishing.adapters.config.platforms import ConfigError, PlatformSettings
from dk_publishing.adapters.platforms.facebook import FacebookPublisher
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.adapters.platforms.meta_credentials import MetaCredentials, SectionTokenProvider
from dk_publishing.adapters.platforms.tokens import FileTokenProvider
from dk_publishing.application.ports import Publisher

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
            credentials = MetaCredentials(Path(one_file))
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
    raise ConfigError(f"platform {name!r} is live but no such adapter is built yet")
