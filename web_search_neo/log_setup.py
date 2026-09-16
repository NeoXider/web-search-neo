"""Per-user diagnostic log for the MCP server process.

The log used to live next to the package, which is read-only (or shared) once
the server is installed from a wheel. It now goes to a per-user state
directory, created on the first record, and logging silently turns itself off
when that location is not writable: a diagnostic log must never stop the
server from starting.

Location, in order of precedence:

- ``WEB_SEARCH_NEO_LOG_FILE`` (a file path);
- Windows: ``%LOCALAPPDATA%\\web-search-neo\\logs\\msp_server.log``;
- elsewhere: ``$XDG_STATE_HOME/web-search-neo/logs/msp_server.log``, by default
  ``~/.local/state/web-search-neo/logs/msp_server.log``.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

LOG_FILE_ENV = "WEB_SEARCH_NEO_LOG_FILE"
LOG_FILE_NAME = "msp_server.log"
APP_DIR_NAME = "web-search-neo"


def default_log_file() -> Path:
    """Where the server log goes when nothing overrides it."""
    configured = os.getenv(LOG_FILE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.getenv("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return base / APP_DIR_NAME / "logs" / LOG_FILE_NAME


class LazyRotatingFileHandler(RotatingFileHandler):
    """A rotating file handler that creates its directory on first use.

    If the directory or file cannot be created, the handler disables itself
    instead of reporting an error for every record.
    """

    def __init__(self, path: Path, *, max_bytes: int = 1_000_000, backup_count: int = 2) -> None:
        self.disabled_reason: str | None = None
        super().__init__(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8", delay=True
        )

    def emit(self, record: logging.LogRecord) -> None:
        if self.disabled_reason is not None:
            return
        if self.stream is None:
            try:
                Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
                self.stream = self._open()
            except OSError as exc:
                self.disabled_reason = f"{type(exc).__name__}: {exc}"
                return
        super().emit(record)


def configure_server_log(name: str = "web_search_neo") -> logging.Logger:
    """Attach the per-user file handler to ``name`` once and return the logger."""
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    if not log.handlers:
        try:
            handler: logging.Handler = LazyRotatingFileHandler(default_log_file())
        except (OSError, ValueError):
            handler = logging.NullHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(handler)
    return log
