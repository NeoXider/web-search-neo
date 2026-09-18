"""Reviewable production size ratchets and package dependency allowlists.

Lower legacy maxima as code is extracted; raising them requires an explicit
architecture review. New production files always use the ordinary hard limit.
"""

SOFT_LIMIT = 600
HARD_LIMIT = 800
LEGACY_MAX_LINES = {
    "web_search_neo/browser_tools.py": 8240,
    "web_search_neo/main.py": 2713,
    "web_search_neo/page_perception.py": 2632,
    "web_search_neo/chrome_bridge.py": 2050,
    "chrome-extension/service-worker.js": 1650,
    "web_search_neo/bridge_daemon.py": 1166,
    "web_search_neo/macros.py": 849,
}

# Each new package may import its own modules, stdlib, and only these internal
# modules/packages. The legacy orchestrators must never be a leaf dependency.
PACKAGE_DEPENDENCIES = {
    "sessions": (),
    "actions": ("sessions", "perception", "cdp", "key_table"),
    "perception": ("page_perception",),
    "cdp": ("chrome_bridge", "chrome_bootstrap", "sessions"),
    "contract": (),
    "fetch": ("web_client",),
}

# Third-party imports are separate so a leaf cannot silently acquire a new
# runtime dependency by importing any arbitrary top-level package.
EXTERNAL_DEPENDENCIES = {"fetch": ("bs4",), "actions": ("selenium.common.exceptions",)}

EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".opencode", "tests", "vendor", "generated",
    "build", "dist",
})
