"""The server entry point: `dk api serve` builds the app from dk.json."""

import logging
from typing import Any

from fastapi import FastAPI

from dk_publishing import composition
from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.application.services import ApiServices
from dk_publishing.entrypoints.publish_api.app import create_app
from dk_publishing.entrypoints.publish_api.review import HideLinkToken

TRUE = ("1", "true", "yes", "on")


def build_app() -> FastAPI:
    env = composition.environment()
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is required and has no default.")
    api = composition.build_api_services(database_url, env)
    review = composition.build_review_services(api, env)
    if review is not None:
        # The sign-in link carries a token in its address: keep it out of the access log.
        logging.getLogger("uvicorn.access").addFilter(HideLinkToken())
    return create_app(api, mcp_endpoint(api, env), review)


def mcp_endpoint(api: ApiServices, env: dict[str, str]) -> Any:
    """The MCP endpoint when dk.json's `mcp.enabled` is true (ADR 0024); otherwise None."""
    if env.get("MCP_ENABLED", "").strip().lower() not in TRUE:
        return None
    from dk_publishing.entrypoints.publish_mcp.server import build_mcp

    disabled = frozenset(
        t.strip() for t in env.get("MCP_DISABLED_TOOLS", "").split(",") if t.strip()
    )
    return build_mcp(api, disabled=disabled).http_app(
        path="/", stateless_http=True, json_response=True
    )
