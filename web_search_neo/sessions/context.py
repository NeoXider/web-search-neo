"""Owned-browser context overrides without access to the session registry."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def apply_overrides(
    driver: Any, profile_mode: str, user_agent: str | None = None,
    timezone: str | None = None, locale: str | None = None,
    geolocation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply explicit CDP overrides only to a browser owned by this server."""
    applied: dict[str, Any] = {}
    wanted = (user_agent, timezone, locale, geolocation)
    if profile_mode in {"current", "attach"} and any(value not in (None, "") for value in wanted):
        raise ValueError(
            f"Fingerprint overrides need an owned browser: profile_mode='{profile_mode}'"
            " shares one real Chrome profile (one fingerprint). Open with"
            " profile_mode='isolated' (one account = one isolated session) to use"
            " user_agent/timezone/locale/geolocation."
        )
    if getattr(driver, "execute_cdp_cmd", None) is None:
        if any(value not in (None, "") for value in wanted):
            raise ValueError("This backend has no CDP channel for fingerprint overrides")
        return applied
    if user_agent or locale:
        effective_agent = str(user_agent or driver.execute_script("return navigator.userAgent"))
        agent_params = {"userAgent": effective_agent}
        if locale:
            agent_params["acceptLanguage"] = str(locale)
        driver.execute_cdp_cmd("Network.setUserAgentOverride", agent_params)
        if user_agent:
            applied["user_agent"] = str(user_agent)
    if timezone:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": str(timezone)})
        applied["timezone"] = str(timezone)
    if locale:
        driver.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": str(locale)})
        applied["locale"] = str(locale)
    if geolocation:
        try:
            lat = float(geolocation.get("latitude"))
            lng = float(geolocation.get("longitude"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(
                "geolocation needs {latitude: float, longitude: float, accuracy?: float}"
            ) from exc
        accuracy = float(geolocation.get("accuracy", 1.0))
        applied["geolocation"] = {"latitude": lat, "longitude": lng, "accuracy": accuracy}
        driver.execute_cdp_cmd("Emulation.setGeolocationOverride", applied["geolocation"])
        # Without the permission getCurrentPosition answered "User denied Geolocation".
        try:
            driver.execute_cdp_cmd("Browser.grantPermissions", {"permissions": ["geolocation"]})
            applied["geolocation"]["permission"] = "granted"
        except Exception as exc:
            applied["geolocation"]["permission"] = f"not granted: {type(exc).__name__}"
    return applied


def resize_viewport(
    driver: Any, profile_mode: str, width: int | None, height: int | None, *,
    set_viewport: Callable[[Any, int, int], Any],
) -> None:
    """Preserve an unspecified dimension; refuse to resize a borrowed window."""
    if width is None and height is None:
        return
    if profile_mode in {"current", "attach"}:
        raise ValueError(
            "Viewport overrides need an owned browser; profile_mode="
            f"'{profile_mode}' shares the user's window."
        )
    viewport = driver.execute_script("return {width: innerWidth, height: innerHeight}") or {}
    current_width = int(width) if width is not None else int(viewport.get("width", 1440))
    current_height = int(height) if height is not None else int(viewport.get("height", 900))
    set_viewport(driver, current_width, current_height)
