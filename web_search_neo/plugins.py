"""Runtime extension registry for Web Search Neo.

Three seams let a plugin widen the server without touching this package: new
actions for ``web_action``, new observation topics for ``web_info``, and new
search providers. Every seam is validated against the same generated schemas
as built-in code, so a typo in a plugin parameter surfaces as the same friendly
error a built-in one would produce.

Load plugins two ways (both at once when both are present):

* ``WEB_SEARCH_NEO_PLUGINS`` - os.pathsep-separated list of .py files or
  directories; a directory loads every non-underscore .py file in it, sorted.
* entry points in the group ``web_search_neo.plugins``.

A plugin either registers at import time by calling the functions below, or
exposes a ``register()`` callable which is invoked after load. Loading fails
fast: one broken plugin names itself in the error instead of dying silently.
"""

import importlib.metadata
import importlib.util
import os
import sys
import types
import typing
from pathlib import Path
from typing import Any, Callable

from web_search_neo.msp_search import register_search_provider  # noqa: F401

ENV_VAR = "WEB_SEARCH_NEO_PLUGINS"
ENTRY_POINT_GROUP = "web_search_neo.plugins"


def _main() -> types.ModuleType:
    """The main module, imported lazily so plugins can load before it is done."""
    from web_search_neo import main as _m

    return _m


def _ensure_legacy_tool(fn: Callable[..., Any]) -> str:
    """Make sure FastMCP can generate a schema for the handler.

    Action and topic validation reads the argument model FastMCP generated for
    the wrapper function, so every plugin handler must be registered on the
    legacy surface even though it is dispatched from the compact one.
    """
    m = _main()
    tools = m.legacy_mcp._tool_manager._tools
    if fn.__name__ not in tools:
        m.legacy_mcp.add_tool(fn)
    return fn.__name__


def register_action(
    name: str,
    handler: Callable[..., Any] | None = None,
    *,
    group: str = "custom",
    summary: str = "",
    overwrite: bool = False,
) -> Callable[..., Any]:
    """Register one callable as a ``web_action`` action.

    The handler's Python signature is the contract: annotated parameters become
    optional (they must have defaults), unannotated ones are refused by the
    generated schema once validation runs. Return a JSON-serialisable dict;
    ``{"success": False, ...}`` marks the step failed without raising. Use as
    ``register_action("name")(fn)`` or pass the handler directly.
    """

    def do_register(fn: Callable[..., Any]) -> Callable[..., Any]:
        m = _main()
        if not isinstance(fn, types.FunctionType):
            raise TypeError(f"action handler for {name!r} must be a function")
        _ensure_legacy_tool(fn)
        spec = m._action(name, fn, group, summary)
        if name in m._ACTIONS and not overwrite:
            raise ValueError(
                f"Action {name!r} is already registered; pass overwrite=True to replace it."
            )
        m._ACTIONS[name] = spec
        return fn

    if handler is not None:
        return do_register(handler)
    return do_register


# Topics named by plugins, in registration order. The built-in set lives in the
# web_info signature; both halves are published together below.
_PLUGIN_TOPICS: list[str] = []


def _web_info_topic_values() -> tuple[str, ...]:
    m = _main()
    hints = typing.get_type_hints(m.web_info)
    base: tuple[Any, ...] = getattr(hints["topic"], "args", ())
    return (*base, *_PLUGIN_TOPICS)


def register_topic(
    name: str,
    handler: Callable[..., Any] | None = None,
    *,
    summary: str = "",
    overwrite: bool = False,
) -> Callable[..., Any]:
    """Register one async callable as a ``web_info`` observation topic.

    The same signature rules as actions apply. Re-registers the compact
    surface's web_info so its published parameter list accepts the new topic;
    dispatch and schema lookup find it like any built-in afterwards.
    """

    def do_register(fn: Callable[..., Any]) -> Callable[..., Any]:
        m = _main()
        if name in m._TOPIC_HANDLERS and not overwrite:
            raise ValueError(
                f"Info topic {name!r} is already registered; pass overwrite=True to replace it."
            )
        doc = (fn.__doc__ or "").strip().splitlines()
        m._INFO_TOPICS[name] = summary or (doc[0].strip() if doc else name)
        m._TOPIC_HANDLERS[name] = fn
        _ensure_legacy_tool(fn)
        if name not in _PLUGIN_TOPICS:
            _PLUGIN_TOPICS.append(name)
        _re_register_web_info()
        return fn

    if handler is not None:
        do_register(handler)
        return handler
    return do_register


def _re_register_web_info() -> None:
    """Publish web_info with the extended topic list so clients validate it."""
    m = _main()
    original = m.web_info
    values = _web_info_topic_values()

    async def web_info(  # noqa: D401 - signature is the contract, keep it bare
        topic: typing.Literal[tuple(values)] = "capabilities",  # type: ignore[valid-type]
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Read one observation; plugin topics included."""
        return await original(topic, params)

    # FastMCP deliberately keeps the first tool on a duplicate add, so use its
    # public removal API before publishing the wrapper with the widened schema.
    m.mcp.remove_tool("web_info")
    m.mcp.add_tool(web_info)


_LOADED: set[str] = set()


def _load_plugin_file(path: Path, origin: str) -> None:
    module_name = f"wsn_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{origin}: cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise RuntimeError(f"{origin}: failed to load {path}: {exc}") from exc
    register = getattr(module, "register", None)
    if callable(register):
        try:
            register()
        except Exception as exc:
            raise RuntimeError(f"{origin}: register() in {path} raised: {exc}") from exc


def _load_plugin_origin(origin: str) -> None:
    path = Path(origin).resolve()
    if path.is_dir():
        for child in sorted(path.glob("*.py")):
            if not child.name.startswith("_"):
                _load_plugin_file(child, origin)
    elif path.suffix == ".py":
        _load_plugin_file(path, origin)
    else:
        raise RuntimeError(f"{origin}: expected a .py file or a directory")


def load_plugins(env_var: str = ENV_VAR) -> list[str]:
    """Load every configured plugin; returns the origins that were loaded.

    Idempotent per origin, so calling it once at startup and again after an
    edit of ``WEB_SEARCH_NEO_PLUGINS`` is safe. Fails fast with the broken
    origin named in the error.
    """
    loaded: list[str] = []
    configured = os.environ.get(env_var, "")
    for entry in configured.split(os.pathsep):
        entry = entry.strip()
        if not entry or entry in _LOADED:
            continue
        try:
            _load_plugin_origin(entry)
        except Exception as exc:
            raise RuntimeError(str(exc)) from None
        _LOADED.add(entry)
        loaded.append(entry)
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        key = f"entry-point:{ep.name}"
        if key in _LOADED:
            continue
        try:
            target = ep.load()
        except Exception as exc:
            raise RuntimeError(f"{key}: entry point failed to load: {exc}") from None
        register = getattr(target, "register", None)
        if callable(register):
            try:
                register()
            except Exception as exc:
                raise RuntimeError(
                    f"{key}: register() raised: {exc}"
                ) from None
        _LOADED.add(key)
        loaded.append(key)
    return loaded


def reset_loaded() -> None:
    """Forget which origins were loaded (test helper; does not unregister)."""
    _LOADED.clear()
