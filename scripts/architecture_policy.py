"""Reviewable production size ratchets and package dependency allowlists.

Lower legacy maxima as code is extracted; raising them requires an explicit
architecture review. New production files always use the ordinary hard limit.
"""

SOFT_LIMIT = 600
HARD_LIMIT = 800
LEGACY_MAX_LINES = {
    # Raised 8305 -> 8360 for the attach_current_tab feature (verified by
    # tests/test_attach_tab*.py); extraction of the parked-tab cluster is pending.
    # Raised 8360 -> 8372 for the click navigation_hint on unverified link clicks.
    # Raised 8372 -> 8503 (2026-10-13) for the local slider-captcha solve: the four
    # browser primitives captcha_solvers needs are injected here, so the maths and
    # the vendor scripts stayed out of this file. Extraction of the pointer cluster
    # is still pending.
    # Raised 8503 -> 8605 (2026-10-13) for two follow-ups on the same feature: the
    # replay sweep's `vary` plumbing, and the free drag that a stalled submit and a
    # challenge wait now try by themselves. Both are adapters over the primitives
    # this file owns - the measuring and the vendor specs stayed in captcha_solvers,
    # the duplicated slider attempt inside `captcha` came out, and most of what is
    # left is the behaviour each path now has to state. The pointer and captcha
    # adapter clusters are still worth extracting.
    # Lowered 8605 -> 8604 (2026-10-14) for 1.27.0: the inline "this captcha cannot be
    # solved" block became one call into captcha.unsolved_report, which now names the
    # vendor and the challenge type. Classification lives in captcha_solvers/classify.py and
    # the protocol reader in captcha_solvers/tc_protocol.py; nothing was added here.
    # Lowered 8604 -> 8516 (2026-10-21): the presence plumbing moved to presence_live.py
    # and the eased pointer approach to pointer_motion.py / actions/pointer_path.py; the
    # freed room also carries the session cap's "unlimited" parsing.
    "web_search_neo/browser_tools.py": 8516,
    # Raised 2680 -> 2706 (2026-10-13) for the update_companion action and its tool:
    # the pull, the refusal rules and the reload all live in companion_update.py, so
    # this file only carries the call and what it says it refuses.
    # Lowered 2706 -> 2701: the macro-validation shape helper moved to
    # web_search_neo/macro_files.py, and the freed lines carry the new
    # screen_state topic wiring (wrapper, registry entry, Literal entry).
    # Lowered 2701 -> 2689: action discriminator diagnostics moved to action_calls.py.
    # Lowered 2689 -> 2678 (2026-10-21): the --bridge/--stop modes moved to run_modes.py,
    # which also carries --node; the freed lines hold the node= forwarding and nodes topic.
    "web_search_neo/main.py": 2678,
    # Raised 2681 -> 2686 for the aria-hidden majority hint in page_text exclusions.
    "web_search_neo/page_perception.py": 2686,
    # Raised 2070 -> 2075 (2026-10-13, from upstream 96427c0): a tab create on a busy
    # machine answered late and left an orphan about:blank behind, so the wait is now
    # configurable. Came in with the rebase; the ratchet follows the file.
    "web_search_neo/chrome_bridge.py": 2075,
    # Raised 1734 -> 1738 (2026-10-13) for the popup Update button's message: it
    # reuses the reload command the daemon already had, so this is one branch.
    # Raised 1738 -> 1741 (2026-10-21) for the popup's Computers tab and Take over: one
    # pause check in handleCommand, the nodes relay hand-off in onmessage, one status
    # field and one import line; the logic lives in nodes.js and control.js.
    # Raised 1741 -> 1742 (2026-10-21) for button pairing: the one spread that registers
    # nodes.js's companion.pairRequest command; the notification lives in nodes.js.
    "chrome-extension/service-worker.js": 1742,
    # Lowered 1163 -> 1162 (2026-10-21): the popup's nodes frames are answered by
    # node_link.py; the one-line hello_ack freed the room for that branch and for
    # the one call that starts this computer's node when the companion connects.
    "web_search_neo/bridge_daemon.py": 1162,
    "web_search_neo/macros.py": 849,
    # Raised 800 -> 817 (2026-10-13) for the badge the user asked to see: a minute
    # of green instead of thirty seconds, and a full-size opaque corner mark. The
    # extra lines are the documented constants and their geometry; the script body
    # itself did not grow. Splitting the in-page script out is worth reviewing.
    # Lowered 817 -> 473 (2026-10-21): the favicon badge (presence_badge.py), the ghost
    # cursor and captions (presence_overlay.py, presence_captions.py) are their own modules.
    "web_search_neo/agent_presence.py": 473,
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
    # Slider captcha solving: pixel maths and a drag path. It takes its browser
    # primitives as callables, which is why it needs no internal module at all.
    "captcha_solvers": (),
    # LAN nodes: the transport, the key files and the node list. The facade glue
    # (forwarding, the MCP wrappers, `wsn --node`) is the top-level node_actions.py.
    "nodes": ("bridge_auth",),
}

# Third-party imports are separate so a leaf cannot silently acquire a new
# runtime dependency by importing any arbitrary top-level package.
# tldextract/publicsuffix2 are optional: cookie_scope uses one only when it is
# already installed and never makes it a requirement.
# numpy is required to measure a captcha picture; PIL decodes it and OpenCV is used
# only for the template matching that needs two images. All three are optional at
# runtime: without them the slider solve reports that it cannot measure, exactly as
# it reports a hole it is not sure about.
EXTERNAL_DEPENDENCIES = {
    "fetch": ("bs4",),
    "actions": ("selenium.common.exceptions", "tldextract", "publicsuffix2"),
    "captcha_solvers": ("numpy", "PIL", "cv2"),
}

EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".opencode", "tests", "vendor", "generated",
    "build", "dist",
})
