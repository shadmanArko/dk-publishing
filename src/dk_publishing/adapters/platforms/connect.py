"""`dk connect <platform>`: one-time setup of a platform's login. Today only YouTube needs it
(Meta tokens are generated in Meta's own tools; see `dk meta`)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.adapters.config.secrets_file import split_ref
from dk_publishing.adapters.platforms.youtube_api import (
    GoogleYouTubeApi,
    YouTubeCredentials,
    run_consent,
    write_template,
)
from dk_publishing.domain.errors import PublishingError


@dataclass
class ConnectResult:
    ok: bool
    lines: list[str]


def connect_platform(
    platform: str,
    path: Path | str,
    *,
    check_only: bool = False,
    consent: Callable[[str, str], str] = run_consent,
    api_factory: Callable[
        [YouTubeCredentials], GoogleYouTubeApi
    ] = GoogleYouTubeApi.from_credentials,
) -> ConnectResult:
    if platform != "youtube":
        raise ConfigError(
            f"{platform!r} has no connect step; Meta tokens are managed with `dk meta`"
        )
    credentials = YouTubeCredentials(path)
    lines: list[str] = []
    if split_ref(path)[1] is None and write_template(split_ref(path)[0]):
        lines.append(f"[did]  created {credentials.path} (owner-only, outside the repo)")
        lines.append("Paste the OAuth client id and secret into it, then run this again.")
        return ConnectResult(True, lines)

    data = credentials.load()
    if not check_only:
        if not (data.get("client_id") and data.get("client_secret")):
            return ConnectResult(
                False, [f"[FAIL] fill in client_id and client_secret in {credentials.path} first"]
            )
        if not data.get("refresh_token"):
            lines.append("[did]  opening your browser for the one-time YouTube consent...")
            try:
                token = consent(str(data["client_id"]), str(data["client_secret"]))
            except PublishingError as exc:
                return ConnectResult(False, [f"[FAIL] {exc}"])
            credentials.update({"refresh_token": token})
            lines.append("[ok]   saved the refresh token")

    if not credentials.load().get("refresh_token"):
        return ConnectResult(
            False, [*lines, "[todo] no refresh_token yet: run `make connect-youtube`"]
        )
    try:
        channel = api_factory(credentials).channel()
    except PublishingError as exc:
        return ConnectResult(False, [*lines, f"[FAIL] YouTube refused the saved login: {exc}"])
    credentials.update({"channel_id": channel[0]})
    lines.append(f"[ok]   connected to the YouTube channel {channel[1]!r} (id {channel[0]})")
    return ConnectResult(True, lines)


DEFAULT_PATH = Path("~/.config/dk-publishing/youtube.json")


def connect_from_env(
    platform: str, env: Mapping[str, str], *, check_only: bool = False
) -> ConnectResult:
    """Where the login file lives comes from the environment; the platform's name stays here."""
    path = env.get("YOUTUBE_CREDENTIALS_FILE", "").strip() or str(DEFAULT_PATH)
    return connect_platform(platform, path, check_only=check_only)
