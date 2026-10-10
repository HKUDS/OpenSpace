# Parallel web search and page fetching

Use [Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp)
through OpenSpace's existing MCP provider for free web search and page fetching,
with no account or API key. Anonymous searches use Fast mode. The free endpoint
is rate limited and intended for exploration and light use.

## Run the example

From the repository root, with Python 3.12 or newer:

```bash
python -m venv .venv-parallel
source .venv-parallel/bin/activate
cd examples/parallel-search
python -m pip install -r requirements.txt
OPENSPACE_SKIP_DOTENV=1 python search.py
```

On Windows, activate with `.venv-parallel\Scripts\activate` and set
`OPENSPACE_SKIP_DOTENV=1` in your shell before running `python search.py`.
The example constrains the MCP SDK to 1.x, which the current OpenSpace
connectors require.

The script loads `config_mcp.json`, discovers the live tools through
`MCPProvider`, calls `web_search` and `web_fetch`, prints their results, and
closes its sessions. It makes direct tool calls without invoking an LLM or
requiring LLM credentials. Customize the search and the page to fetch:

```bash
OPENSPACE_SKIP_DOTENV=1 python search.py \
  --query "Python asyncio TaskGroup cancellation" \
  --url https://docs.python.org/3/library/asyncio-task.html
```

## Add the server to your OpenSpace configuration

Merge the `parallel` entry from this example's `mcpServers` into your
`openspace/config/config_mcp.json`, retaining any existing entries. OpenSpace's
HTTP connector negotiates Streamable HTTP with `https://search.parallel.ai/mcp`;
the entry includes an OpenSpace User-Agent and no authentication header.
The example does not change the built-in `web_search` provider. Parallel's
`web_search` and `web_fetch` are external MCP tools.
