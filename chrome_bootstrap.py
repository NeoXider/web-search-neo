"""Opt-in Windows UI bootstrap for the Web Search Neo Chrome companion."""

from __future__ import annotations

from pathlib import Path
import platform
import time
from typing import Any

import bridge_auth
from chrome_bridge import CHROME_EXTENSION_ID, get_chrome_bridge


EXTENSION_NAME = "Web Search Neo Companion"
EXTENSION_DIR = (Path(__file__).resolve().parent / "chrome-extension").resolve()

# pywinauto reads these as modifiers and grouping, so a repository cloned into a
# path like "C:\Git\Web (new)+tools" would type Ctrl and Alt into Chrome's dialog
# instead of the path.
_SEND_KEYS_SPECIAL = "^%+~(){}[]"


def _escape_send_keys(text: str) -> str:
    """Escape pywinauto's control characters so a literal string is typed."""
    return "".join(f"{{{character}}}" if character in _SEND_KEYS_SPECIAL else character
                   for character in text)


def _control_text(control: Any) -> str:
    try:
        return str(control.window_text() or "").strip().casefold()
    except Exception:
        return ""


def _find_control(root: Any, names: tuple[str, ...], control_type: str | None = None) -> Any | None:
    expected = tuple(name.casefold() for name in names)
    try:
        controls = root.descendants(control_type=control_type) if control_type else root.descendants()
    except Exception:
        return None
    for control in controls:
        text = _control_text(control)
        if text and any(name == text or name in text for name in expected):
            return control
    return None


def _process_executable(process_id: int) -> Path | None:
    """Read one Windows process path without adding a runtime dependency."""
    try:
        import ctypes
        from ctypes import wintypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(process_id))
        if not process:
            return None
        try:
            capacity = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(capacity)
            ):
                return None
            return Path(buffer.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    except Exception:
        return None


def _select_chrome_window(desktop: Any, window_title: str | None) -> tuple[Any, Path]:
    selected_title = (window_title or "").strip().casefold()
    candidates: list[tuple[Any, Path]] = []
    for window in desktop.windows():
        try:
            if not window.is_visible():
                continue
            executable = _process_executable(int(window.element_info.process_id))
        except Exception:
            continue
        if executable is None:
            continue
        normalized = str(executable).replace("/", "\\").casefold()
        if not normalized.endswith(r"\google\chrome\application\chrome.exe"):
            continue
        if selected_title and selected_title not in _control_text(window):
            continue
        candidates.append((window, executable))
    if not candidates:
        suffix = f" matching title '{window_title}'" if window_title else ""
        raise RuntimeError(f"No visible Google Chrome window was found{suffix}")
    if len(candidates) > 1:
        titles = [str(window.window_text() or "") for window, _ in candidates]
        raise RuntimeError(
            "More than one Google Chrome window is open; repeat setup_current_chrome "
            f"with an exact window_title. Candidates: {titles}"
        )
    return candidates[0]


def _visible_top_level_windows() -> list[tuple[int, str]]:
    """Enumerate visible top-level windows and their titles through win32.

    pywinauto's UIA desktop enumeration does not list Chrome's folder picker on
    current Windows builds, so waiting for the dialog through it never succeeds
    even while the dialog is plainly on screen.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(handle, _parameter):
        if user32.IsWindowVisible(handle):
            length = user32.GetWindowTextLengthW(handle)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(handle, buffer, length + 1)
                found.append((int(handle), buffer.value))
        return True

    user32.EnumWindows(collect, 0)
    return found


_PICKER_TITLES = (
    "select the extension directory",
    "select extension directory",
    "выберите каталог расширения",
    "выберите папку расширения",
    "обзор папок",
)


def _wait_for_dialog(known_handles: set[int], timeout_seconds: float) -> int:
    """Return the picker's window handle.

    The handle is used directly rather than through a pywinauto wrapper: this
    dialog cannot be wrapped by the UIA backend on current Windows builds, which
    is why waiting for it used to time out while it sat on screen.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for handle, title in _visible_top_level_windows():
            normalized = title.strip().casefold()
            if handle not in known_handles and any(
                marker in normalized for marker in _PICKER_TITLES
            ):
                return handle
        time.sleep(0.2)
    raise TimeoutError("Chrome extension directory picker did not open")


def _focus_window(handle: int) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    user32.ShowWindow(handle, 9)  # SW_RESTORE
    user32.SetForegroundWindow(handle)
    time.sleep(0.3)


def _window_is_open(handle: int) -> bool:
    return any(found == handle for found, _title in _visible_top_level_windows())


def _try_enable_existing(extension_window: Any, reload_existing: bool) -> str | None:
    marker = _find_control(extension_window, (CHROME_EXTENSION_ID, EXTENSION_NAME))
    if marker is None:
        return None

    node = marker
    for _ in range(8):
        try:
            candidates = node.descendants()
        except Exception:
            candidates = []
        toggles: list[tuple[Any, int]] = []
        for control in candidates:
            if "developer mode" in _control_text(control) or "режим разработчика" in _control_text(control):
                continue
            try:
                state = int(control.get_toggle_state())
            except Exception:
                continue
            toggles.append((control, state))
        if len(toggles) == 1:
            control, state = toggles[0]
            if state == 0:
                control.click_input()
                return "enabled_existing"
            if reload_existing:
                reload_button = _find_control(
                    node,
                    ("reload", "перезагрузить"),
                    "Button",
                )
                if reload_button is None:
                    raise RuntimeError(
                        "The exact companion card was found, but its Reload button was not accessible"
                    )
                reload_button.click_input()
                return "reloaded_existing"
            return "already_enabled"
        try:
            node = node.parent()
        except Exception:
            break
    raise RuntimeError(
        "The companion card was found, but its exact enable switch could not be isolated safely"
    )


def _install_with_windows_ui(
    extension_dir: Path,
    timeout_seconds: float,
    window_title: str | None,
    reload_existing: bool,
) -> dict[str, Any]:
    try:
        from pywinauto import Desktop
        from pywinauto.keyboard import send_keys
    except ImportError as exc:
        raise RuntimeError(
            "pywinauto is required for setup_current_chrome on Windows; "
            "run: python -m pip install -r requirements.txt"
        ) from exc

    desktop = Desktop(backend="uia")
    extension_window, chrome = _select_chrome_window(desktop, window_title)
    # pywinauto's set_focus does not reliably raise a minimised or background
    # Chrome, and the keystrokes below go wherever the foreground window is.
    try:
        _focus_window(int(extension_window.handle))
    except Exception:
        pass
    extension_window.set_focus()
    send_keys("^l")
    send_keys(_escape_send_keys("chrome://extensions/"), with_spaces=True)
    send_keys("{ENTER}")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if any(
            marker in _control_text(extension_window)
            for marker in ("extensions", "расширения")
        ):
            break
        time.sleep(0.2)
    else:
        raise TimeoutError("The selected Chrome window did not open chrome://extensions")

    existing_action = _try_enable_existing(extension_window, reload_existing)
    if existing_action:
        return {
            "ui_action": existing_action,
            "chrome_executable": str(chrome),
            "chrome_window_title": str(extension_window.window_text() or ""),
        }

    load_names = ("load unpacked", "загрузить распакованное расширение")
    load_button = _find_control(extension_window, load_names, "Button")
    if load_button is None:
        developer_toggle = _find_control(
            extension_window,
            ("developer mode", "режим разработчика"),
        )
        if developer_toggle is None:
            raise RuntimeError("Chrome Developer mode toggle was not found")
        try:
            enabled = int(developer_toggle.get_toggle_state()) == 1
        except Exception:
            enabled = False
        if not enabled:
            developer_toggle.click_input()
        deadline = time.monotonic() + min(timeout_seconds, 10.0)
        while time.monotonic() < deadline and load_button is None:
            time.sleep(0.2)
            load_button = _find_control(extension_window, load_names, "Button")
    if load_button is None:
        raise RuntimeError("Chrome Load unpacked button was not found")

    known_handles = {handle for handle, _title in _visible_top_level_windows()}
    load_button.click_input()
    picker = _wait_for_dialog(known_handles, min(timeout_seconds, 15.0))
    _focus_window(picker)

    # Alt+D focuses the picker's path box; entering a full path selects that
    # folder. A second Enter confirms if the dialog is still up.
    send_keys("%d")
    send_keys(_escape_send_keys(str(extension_dir)), with_spaces=True)
    send_keys("{ENTER}")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _window_is_open(picker):
        send_keys("{ENTER}")
        time.sleep(0.5)
    if _window_is_open(picker):
        raise TimeoutError("The extension directory picker did not accept the folder")

    return {
        "ui_action": "loaded_unpacked",
        "chrome_executable": str(chrome),
        "chrome_window_title": str(extension_window.window_text() or ""),
    }


def prepare_bridge_token() -> dict[str, Any]:
    """Make sure the shared secret exists on disk and inside the extension folder."""
    try:
        token = bridge_auth.load_or_create_token()
        return {"token_ready": True, "token_file": str(bridge_auth.write_extension_token(token))}
    except Exception as exc:
        return {"token_ready": False, "token_error": f"{type(exc).__name__}: {exc}"}


def expected_extension_version() -> str:
    try:
        import json

        return str(json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))["version"])
    except Exception:
        return ""


def setup_current_chrome(
    confirm_install: bool = False,
    timeout_seconds: float = 30.0,
    window_title: str | None = None,
) -> dict[str, Any]:
    """Install/enable the companion in the current Chrome after explicit consent."""
    timeout = max(5.0, min(float(timeout_seconds), 120.0))
    # The companion refuses to talk to the bridge without the shared secret, so
    # the token has to be on disk before Chrome ever loads the folder.
    token_info = prepare_bridge_token()
    bridge = get_chrome_bridge()
    bridge.start()
    status = bridge.status(1.0)
    expected_version = expected_extension_version()
    connected_version = str(status.get("browser", {}).get("extension_version") or "")
    version_current = bool(expected_version and connected_version == expected_version)
    if status["connected"] and version_current:
        return {
            "success": True,
            "already_connected": True,
            "confirmation_required": False,
            "extension_directory": str(EXTENSION_DIR),
            "extension_version": expected_version,
            "current_chrome": status,
            **token_info,
        }

    if not confirm_install:
        return {
            "success": True,
            "ready": False,
            "already_connected": False,
            "confirmation_required": True,
            "confirmation_prompt": (
                "Allow Web Search Neo to enable Chrome Developer mode and load the "
                f"unpacked extension from {EXTENSION_DIR}?"
            ),
            "extension_directory": str(EXTENSION_DIR),
            "extension_version": expected_version,
            "connected_version": connected_version or None,
            "update_required": bool(status["connected"] and not version_current),
            "current_chrome": status,
            **token_info,
        }

    if platform.system() != "Windows":
        return {
            "success": False,
            "already_connected": False,
            "confirmation_required": False,
            "manual_install_required": True,
            "error": "Automatic companion setup is currently supported only on Windows",
            "extension_directory": str(EXTENSION_DIR),
            "current_chrome": status,
            **token_info,
        }
    if not (EXTENSION_DIR / "manifest.json").is_file():
        raise FileNotFoundError(f"Companion manifest is missing: {EXTENSION_DIR}")

    try:
        ui_result = _install_with_windows_ui(
            EXTENSION_DIR,
            timeout,
            window_title,
            # We only get here when the expected build is not connected, and an
            # already-enabled card re-reads bridge-token.js only on reload.
            reload_existing=True,
        )
        connected = bridge.wait_connected(timeout)
        final_status = bridge.status(0.0)
        final_version = str(
            final_status.get("browser", {}).get("extension_version") or ""
        )
        ready = bool(connected and final_version == expected_version)
        return {
            "success": ready,
            "ready": ready,
            "already_connected": False,
            "confirmation_required": False,
            "manual_install_required": not ready,
            "extension_directory": str(EXTENSION_DIR),
            "current_chrome": final_status,
            **token_info,
            **ui_result,
            **(
                {}
                if ready
                else {
                    "error": (
                        "Chrome UI completed, but the expected companion build did not connect. "
                        f"Expected {expected_version or 'the bundled version'}, got "
                        f"{final_version or 'no connection'}. Keep the extension enabled and "
                        "repeat setup to reload the exact companion card."
                    )
                }
            ),
        }
    except Exception as exc:
        return {
            "success": False,
            "already_connected": False,
            "confirmation_required": False,
            "manual_install_required": True,
            "extension_directory": str(EXTENSION_DIR),
            "current_chrome": bridge.status(0.0),
            "error": f"{type(exc).__name__}: {exc}",
            **token_info,
        }
