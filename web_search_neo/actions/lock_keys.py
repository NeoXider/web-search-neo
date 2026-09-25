"""CapsLock, NumLock and ScrollLock: real key events, and the lock state Chrome does not keep.

WebDriver has no code point for the lock keys, so they are sent as CDP
``Input.dispatchKeyEvent`` keyDown/keyUp: the companion's driver sends every key
that way already, a Selenium session through ``execute_cdp_cmd``. The page gets a
real keydown/keyup pair - key and code ``CapsLock``/``NumLock``/``ScrollLock``,
keyCode 20/144/145, location 0.

Chrome keeps no lock state for synthetic input. Measured on Chrome 153, headless
and headful: after any CDP CapsLock ``getModifierState('CapsLock')`` stays false,
and ``modifiers`` has no lock bit (an unknown bit is dropped). So the session
keeps the toggle itself, flipped on every keyDown that is not an auto-repeat.
While CapsLock is on a letter key reports what a keyboard with CapsLock on
reports: ``key`` and the typed ``text`` upper-case (lower-case under Shift),
``code``/keyCode unchanged, ``shiftKey`` only while Shift really is down. On the
Selenium path those letters ride CDP as well, because chromedriver reports every
capital it types as shifted. ``type_text`` marks its events ``exact``: text is
typed as written, whatever the lock. NumLock and ScrollLock only flip their
tracked state; the NUMPAD0-9 names always send digits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable
import weakref

from web_search_neo import key_table


@dataclass
class LockState:
    """One driver's locks, and what the Selenium path needs to keep CDP and WebDriver apart."""

    on: set[str] = field(default_factory=set)
    # Modifier bits down as this stream sent them, for the letters sent through CDP.
    modifiers: int = 0
    # Physical keys whose keyDown went through CDP: their repeat and keyUp must too,
    # since chromedriver drops a keyUp for a key it never pressed.
    via_cdp: set[str] = field(default_factory=set)


_STATES: "weakref.WeakKeyDictionary[Any, LockState]" = weakref.WeakKeyDictionary()


def state_of(driver: Any) -> LockState:
    """The lock state of the session behind ``driver``; it lives as long as the driver."""
    try:
        return _STATES.setdefault(driver, LockState())
    except TypeError:  # a driver that cannot be weakly referenced keeps no state
        return LockState()


def locks_on(driver: Any) -> list[str]:
    """The locks this session has toggled on (every lock starts off)."""
    return sorted(state_of(driver).on)


def _is_letter(key: str) -> bool:
    return len(key) == 1 and "a" <= key.lower() <= "z"


def _annotate(events: list[dict[str, Any]], state: LockState) -> list[dict[str, Any]]:
    """Flip the locks the stream presses and mark the letters CapsLock changes."""
    marked: list[dict[str, Any]] = []
    for event in events:
        key = str(event.get("key", ""))
        if event["type"] == "down" and key in key_table.LOCK_KEYS and not event.get("repeat"):
            state.on ^= {key}
        elif event["type"] != "pause" and not event.get("exact") and _is_letter(key) \
                and "CapsLock" in state.on:
            event = {**event, "caps": True}
        marked.append(event)
    return marked


def _without_cdp(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refuse a lock key down before anything is sent; a lock key up had nothing to lift."""
    for event in events:
        name = str(event.get("key", ""))
        if event["type"] == "down" and name in key_table.LOCK_KEYS:
            raise ValueError(f"Unsupported key '{name}': {key_table.UNSENDABLE_KEYS[name.upper()]}.")
    return [e for e in events if str(e.get("key", "")) not in key_table.LOCK_KEYS]


def _routes_to_cdp(event: dict[str, Any], state: LockState) -> bool:
    key = str(event["key"])
    if key in key_table.LOCK_KEYS:
        return True
    physical = key_table.physical_key(key)
    if physical in state.via_cdp:
        if event["type"] == "up":
            state.via_cdp.discard(physical)
        return True
    if event["type"] == "down" and event.get("caps") and not event.get("repeat"):
        state.via_cdp.add(physical)
        return True
    return False


def perform(driver: Any, events: list[dict[str, Any]], chain_factory: Callable[[Any], Any]) -> None:
    """Send one ordered key stream; the lock keys, and CapsLock's letters, through CDP.

    A driver with ``perform_key_events`` (the companion) sends everything itself.
    A Selenium driver sends through ``chain_factory(driver)`` - ActionChains - in
    runs, with the CDP events between them in stream order. Nothing changes for a
    stream that touches no lock while every lock is off.
    """
    cdp = getattr(driver, "execute_cdp_cmd", None)
    if not callable(cdp):
        events = _without_cdp(events)
    state = state_of(driver)
    events = _annotate(events, state)
    if hasattr(driver, "perform_key_events"):
        driver.perform_key_events(events)
        return
    runs: list[tuple[bool, list[Any]]] = []
    for event in events:
        if event["type"] == "pause":
            item: Any = event
            to_cdp = runs[-1][0] if runs else False
        else:
            params, state.modifiers = key_table.cdp_key_event(event, state.modifiers)
            to_cdp = _routes_to_cdp(event, state)
            item = params if to_cdp else event
        if not runs or runs[-1][0] != to_cdp:
            runs.append((to_cdp, []))
        runs[-1][1].append(item)
    for to_cdp, items in runs or [(False, [])]:
        if to_cdp:
            for params in items:
                if params.get("type") == "pause":
                    time.sleep(max(0.0, float(params.get("seconds", 0.0))))
                else:
                    cdp("Input.dispatchKeyEvent", params)
            continue
        chain = chain_factory(driver)
        for event in items:
            if event["type"] == "down":
                chain.key_down(event["key"])
            elif event["type"] == "up":
                chain.key_up(event["key"])
            elif event["type"] == "pause":
                chain.pause(float(event.get("seconds", 0.0)))
        chain.perform()
