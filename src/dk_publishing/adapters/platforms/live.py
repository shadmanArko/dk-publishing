"""Build the real publisher for a platform that is switched to `live`.

Interim credentials: the Page id and a token file named in the environment. The encrypted token
vault replaces this when the connect flow exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import httpx

from dk_publishing.adapters.config.platforms import ConfigError, PlatformSettings
from dk_publishing.adapters.platforms.facebook import FacebookPublisher
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.adapters.platforms.tokens import FileTokenProvider
from dk_publishing.application.ports import Publisher

DEFAULT_GRAPH_VERSION = "v25.0"


def build_live_publisher(
    name: str,
    settings: PlatformSettings,
    env: Mapping[str, str],
    transport: httpx.BaseTransport | None = None,
) -> Publisher:
    # Native scheduling is not built, so a live post is published by this system at its slot.
    caps = replace(settings.capabilities, native_window=None)
    if name == "facebook":
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
            raise ConfigError(f"facebook is live but {', '.join(missing)} is not set in .env")
        graph = GraphClient(
            version=settings.api_version or DEFAULT_GRAPH_VERSION,
            tokens=FileTokenProvider(Path(token_file).expanduser()),
            transport=transport,
        )
        return FacebookPublisher(page_id=page_id, capabilities=caps, graph=graph)
    raise ConfigError(f"platform {name!r} is live but no such adapter is built yet")
