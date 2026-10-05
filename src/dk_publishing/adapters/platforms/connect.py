"""`dk connect <platform>`: one-time setup of a platform's login. Today only YouTube needs it
(Meta tokens are generated in Meta's own tools; see `dk meta`)."""

from __future__ import annotations

import secrets
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.adapters.config.secrets_file import split_ref
from dk_publishing.adapters.platforms.tiktok_api import (
    HttpTikTokApi,
    TikTokCredentials,
    TikTokTokens,
    authorize_url,
    exchange_code,
)
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
    if platform == "tiktok":
        return connect_tiktok(path, check_only=check_only)
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


def connect_tiktok(
    path: Path | str,
    *,
    check_only: bool = False,
    again: bool = False,
    ask: Callable[[str], str] = input,
    open_browser: Callable[[str], object] = webbrowser.open,
    transport: httpx.BaseTransport | None = None,
    state: str | None = None,
) -> ConnectResult:
    """Sign in to TikTok once. The browser asks you to allow the app, then lands on the redirect
    address with a one-time code in it; you paste that address back here."""
    credentials = TikTokCredentials(path)
    data = credentials.load()
    lines: list[str] = []
    if not check_only:
        try:
            key, _ = credentials.app()
        except ConfigError as exc:
            return ConnectResult(False, [f"[FAIL] {exc}"])
        redirect = str(data.get("redirect_uri") or "").strip()
        if not redirect:
            return ConnectResult(
                False, [f"[FAIL] fill in tiktok.redirect_uri in {credentials.path}"]
            )
        if again or not data.get("refresh_token") or not data.get("access_token"):
            token = state or secrets.token_urlsafe(24)
            url = authorize_url(key, redirect, token, always_ask=again)
            lines.append("[did]  opening your browser: sign in to TikTok and allow the app...")
            open_browser(url)
            pasted = ask(
                "After allowing, copy the FULL address of the page you land on and paste it here: "
            ).strip()
            query = parse_qs(urlparse(pasted).query)
            if query.get("error"):
                return ConnectResult(False, [*lines, f"[FAIL] TikTok said: {query['error'][0]}"])
            if query.get("state", [""])[0] != token:
                return ConnectResult(
                    False,
                    [*lines, "[FAIL] that address does not belong to this sign-in; start again"],
                )
            code = query.get("code", [""])[0]
            if not code:
                return ConnectResult(False, [*lines, "[FAIL] the address has no code=... in it"])
            try:
                exchange_code(credentials, code, redirect, transport=transport)
            except PublishingError as exc:
                return ConnectResult(False, [*lines, f"[FAIL] {exc}"])
            lines.append("[ok]   saved the TikTok login")
    if not credentials.load().get("refresh_token"):
        return ConnectResult(
            False, [*lines, "[todo] no TikTok login yet: run `make connect-tiktok`"]
        )
    try:
        info = HttpTikTokApi(TikTokTokens(credentials, transport), transport).creator_info()
    except PublishingError as exc:
        return ConnectResult(False, [*lines, f"[FAIL] TikTok refused the saved login: {exc}"])
    options = ", ".join(info.privacy_options) or "none"
    lines.append(
        f"[ok]   connected to TikTok as @{info.username} ({info.nickname}); allowed: {options}"
    )
    return ConnectResult(True, lines)


def connect_from_env(
    platform: str, env: Mapping[str, str], *, check_only: bool = False, again: bool = False
) -> ConnectResult:
    """Where the login file lives comes from the environment; the platform's name stays here."""
    if platform == "tiktok":
        ref = env.get("TIKTOK_CREDENTIALS_FILE", "").strip()
        if not ref:
            raise ConfigError("set DK_CONFIG_FILE (run `make init`): TikTok lives in dk.json")
        return connect_tiktok(ref, check_only=check_only, again=again)
    path = env.get("YOUTUBE_CREDENTIALS_FILE", "").strip() or str(DEFAULT_PATH)
    return connect_platform(platform, path, check_only=check_only)
