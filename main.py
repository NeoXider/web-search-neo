"""Compatibility shim for the packaged MCP server."""

from web_search_neo.main import *
from web_search_neo.main import main as main


if __name__ == "__main__":
    main()
