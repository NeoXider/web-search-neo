# PythonUrlFeatch

Python URL fetcher with MCP‑based tools for fetching URLs, extracting text, and web searching.

## Installation

```bash
# Clone the repository
git clone https://github.com/your-username/PythonUrlFeatch.git
cd PythonUrlFeatch

# Install required packages
pip install -r requirements.txt
```

## Running the MCP Server

```bash
python msp_server.py
```

The server exposes the following tools via the MCP protocol:

- `fetch_url_text(url)` – get plain text from a URL.
- `fetch_page_links(url)` – list links on a page.
- `search_duckduckgo(query, num)` – DuckDuckGo search.
- `search_yandex(query, num)` – Yandex search.
- `get_current_time_and_region()` – local date/time.

## CLI Usage

```bash
python msp_server.py --urls https://example.com https://another.org
```

Logs are written to `msp_server.log`.

## MCP API Example

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("URL Text Fetcher")
text = mcp.call("fetch_url_text", url="https://example.com")
print(text[:200])
```

## Example Configuration (msp.json)

A sample configuration file `msp.json` can be used to customize the MCP server:

```json
{
  "mcpServers": {
    "web-search-neo": {
      "command": "python",
      "args": [
        "PATH/main.py"
      ]
    }
  }
}
```

Place this file in the project root to customize server settings.

## Contributing

Fork, pull requests, issues. Follow style and tests.
