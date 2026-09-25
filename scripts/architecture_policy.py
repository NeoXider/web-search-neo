"""Reviewable production size ratchets and package dependency allowlists.

Lower legacy maxima as code is extracted; raising them requires an explicit
architecture review. New production files always use the ordinary hard limit.
"""

SOFT_LIMIT = 600
HARD_LIMIT = 800
LEGACY_MAX_LINES = {
    # Raised 8305 -> 8360 for the attach_current_tab feature (verified by
    # tests/test_attach_tab*.py); extraction of the parked-tab cluster is pending.
    "web_search_neo/browser_tools.py": 8360,
    "web_search_neo/main.py": 2679,
    "web_search_neo/page_perception.py": 2681,
    "web_search_neo/chrome_bridge.py": 2070,
    "chrome-extension/service-worker.js": 1734,
    "web_search_neo/bridge_daemon.py": 1163,
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
    # Passive site checks: pure analysis plus plain GETs through the shared client.
    "audit": ("web_client", "fetch", "actions.cookie_scope"),
}

# Third-party imports are separate so a leaf cannot silently acquire a new
# runtime dependency by importing any arbitrary top-level package.
# tldextract/publicsuffix2 are optional: cookie_scope uses one only when it is
# already installed and never makes it a requirement.
EXTERNAL_DEPENDENCIES = {
    "fetch": ("bs4",),
    "actions": ("selenium.common.exceptions", "tldextract", "publicsuffix2"),
}

EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".opencode", "tests", "vendor", "generated",
    "build", "dist",
})
