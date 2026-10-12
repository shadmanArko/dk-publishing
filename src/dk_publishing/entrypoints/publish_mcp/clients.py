"""Ready-to-paste MCP client settings (`dk api mcp-config`). They name the key only as the
environment variable DK_PUBLISH_KEY: a key is never printed, written or passed on a command line."""

from __future__ import annotations

import json

CLIENTS = ("claude-code", "claude-desktop", "cursor", "langchain", "openai-agents")
KEY_VAR = "DK_PUBLISH_KEY"


def endpoint(api_site: str) -> str:
    site = api_site.strip().removeprefix("https://").removeprefix("http://").strip("/")
    return f"https://{site or 'publish.example.com'}/mcp/"


def client_config(client: str, url: str) -> str:
    header = f"Bearer ${{{KEY_VAR}}}"
    if client == "claude-code":
        config = {"mcpServers": {"dk_publish": {"type": "http", "url": url,
                                                "headers": {"Authorization": header}}}}  # fmt: skip
        return (
            f"# .mcp.json in your project (Claude Code expands ${{{KEY_VAR}}} from your shell):\n"
            f"{json.dumps(config, indent=2)}\n"
        )
    if client == "cursor":
        config = {"mcpServers": {"dk_publish": {"url": url, "headers": {"Authorization": header}}}}
        return f"# ~/.cursor/mcp.json:\n{json.dumps(config, indent=2)}\n"
    if client == "claude-desktop":
        config = {
            "mcpServers": {
                "dk_publish": {
                    "command": "npx",
                    "args": ["-y", "mcp-remote", url, "--header", f"Authorization:{header}"],
                    "env": {KEY_VAR: "<paste the key here, in this file only>"},
                }
            }
        }
        return (
            "# claude_desktop_config.json (Desktop reaches remote servers through mcp-remote;\n"
            "# pin the mcp-remote version you reviewed, e.g. mcp-remote@<version>):\n"
            f"{json.dumps(config, indent=2)}\n"
        )
    if client == "langchain":
        return f'''# pip install langchain-mcp-adapters
import os
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({{"dk_publish": {{
    "transport": "streamable_http",
    "url": "{url}",
    "headers": {{"Authorization": f"Bearer {{os.environ['{KEY_VAR}']}}"}},
}}}})
tools = await client.get_tools()
'''
    if client == "openai-agents":
        return f'''# pip install openai-agents
import os
from agents.mcp import MCPServerStreamableHttp

dk_publish = MCPServerStreamableHttp(params={{
    "url": "{url}",
    "headers": {{"Authorization": f"Bearer {{os.environ['{KEY_VAR}']}}"}},
}})
'''
    raise ValueError(f"client must be one of: {', '.join(CLIENTS)}")
