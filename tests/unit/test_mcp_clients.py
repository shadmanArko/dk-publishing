"""`dk api mcp-config`: every client's settings point at the endpoint and never contain a key."""

from __future__ import annotations

import pytest

from dk_publishing.entrypoints.cli import main
from dk_publishing.entrypoints.publish_mcp.clients import CLIENTS, client_config, endpoint


def test_the_endpoint_is_https_with_the_trailing_slash() -> None:
    assert endpoint("publish.example.org") == "https://publish.example.org/mcp/"
    assert endpoint("https://publish.example.org/") == "https://publish.example.org/mcp/"
    assert endpoint("") == "https://publish.example.com/mcp/"


@pytest.mark.parametrize("client", CLIENTS)
def test_every_client_names_the_key_only_as_a_variable(client: str) -> None:
    text = client_config(client, "https://publish.example.org/mcp/")
    assert "https://publish.example.org/mcp/" in text and "DK_PUBLISH_KEY" in text
    assert "dkp_" not in text


def test_the_command_needs_no_database(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DK_CONFIG_FILE", raising=False)
    assert main(["api", "mcp-config", "--client", "cursor", "--site", "publish.example.org"]) == 0
    assert "https://publish.example.org/mcp/" in capsys.readouterr().out
    assert main(["api", "mcp-config", "--client", "nope"]) == 1
