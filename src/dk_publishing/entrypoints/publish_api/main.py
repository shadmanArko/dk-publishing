"""The server entry point: `dk api serve` builds the app from dk.json."""

from fastapi import FastAPI

from dk_publishing import composition
from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.entrypoints.publish_api.app import create_app


def build_app() -> FastAPI:
    env = composition.environment()
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is required and has no default.")
    return create_app(composition.build_api_services(database_url, env))
