"""Search and fetch through OpenSpace's MCP provider, without an LLM call."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from openspace.grounding.backends.mcp import MCPProvider
from openspace.grounding.core.types import ToolStatus


async def search_and_fetch(query: str, url: str) -> None:
    config = json.loads(Path(__file__).with_name("config_mcp.json").read_text())
    provider = MCPProvider(config)
    session_id = uuid4().hex
    try:
        tools = {tool.name for tool in await provider.list_tools(use_cache=False)}
        if not {"web_search", "web_fetch"}.issubset(tools):
            raise RuntimeError("Parallel search and fetch tools are unavailable")

        calls = [
            ("web_search", {"objective": query, "search_queries": [query]}),
            ("web_fetch", {"urls": [url], "objective": query[:200]}),
        ]
        for name, parameters in calls:
            result = await provider.call_tool(
                "mcp-parallel", name, {**parameters, "session_id": session_id}
            )
            if result.status != ToolStatus.SUCCESS:
                raise RuntimeError(f"{name} failed: {result.error or result.content}")
            if not result.content:
                raise RuntimeError(f"{name} returned no content")
            print(f"\n{name}:\n{result.content}")
    finally:
        await provider.close_all_sessions()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="Python asyncio task cancellation")
    parser.add_argument("--url", default="https://docs.python.org/3/library/asyncio-task.html")
    args = parser.parse_args()
    asyncio.run(search_and_fetch(args.query, args.url))


if __name__ == "__main__":
    main()
