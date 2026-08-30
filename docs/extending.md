# Extending Web Search Neo

Plugins can add compact `web_action` actions, `web_info` observation topics,
and search providers without changing Web Search Neo itself. A plugin is an
ordinary Python module loaded when the server starts.

## Loading plugins from files

Set `WEB_SEARCH_NEO_PLUGINS` to an `os.pathsep`-separated list of `.py` files
and directories. On Windows the separator is `;`; on Linux and macOS it is
`:`. Each directory contributes every direct `*.py` child whose name does not
start with `_`, in sorted filename order.

```powershell
$env:WEB_SEARCH_NEO_PLUGINS = @(
  "C:\work\web-search-neo-plugins\greeting.py",
  "C:\work\web-search-neo-plugins\local"
) -join [IO.Path]::PathSeparator
python main.py
```

File plugins may register while they are imported or expose a callable
`register()` function. If a file cannot be imported, or its `register()`
raises, startup stops immediately and the error names the broken file or
directory origin. One bad plugin is never silently skipped.

## Loading plugins from a package

Installed packages can publish an entry point in the
`web_search_neo.plugins` group:

```toml
[project.entry-points."web_search_neo.plugins"]
greeting = "greeting_plugin"
```

The loaded object is normally the plugin module. It can register as an import
side effect, or it can expose a callable `register()` that Web Search Neo calls
after loading it. Choose one form for a given registration so the same name is
not registered twice. Entry-point import and `register()` failures are also
fail-fast and name the broken entry point.

The environment variable and entry-point mechanisms are cumulative: when both
are configured, Web Search Neo loads both.

## Adding an action

`register_action` has this signature:

```text
register_action(
    name,
    handler=None,
    *,
    group="custom",
    summary="",
    overwrite=False,
)
```

With no `handler`, it returns a decorator. Passing the handler directly
registers the same function immediately. `name` is the value clients put in
the `action` field, `group` organizes capability discovery, and `summary` is
the short description shown there. Existing names are refused unless
`overwrite=True` is explicit.

This is a complete plugin file:

```python
from typing import Any

from web_search_neo.plugins import register_action


@register_action(
    "greet",
    group="custom",
    summary="Return a greeting without opening a browser.",
)
async def greet(name: str = "world") -> dict[str, Any]:
    return {"message": f"Hello, {name}!"}
```

Save it as `greeting.py`, add that path to `WEB_SEARCH_NEO_PLUGINS`, and call:

```json
{"actions":[{"action":"greet","name":"Ada"}]}
```

Direct registration is equivalent:

```python
from typing import Any

from web_search_neo.plugins import register_action


async def wave(name: str = "world") -> dict[str, Any]:
    return {"message": f"Hello, {name}!"}


register_action(
    "wave",
    wave,
    group="custom",
    summary="Return a greeting without opening a browser.",
)
```

Handlers are async functions. Every exposed parameter must have a type
annotation and a default value; it therefore appears as an optional parameter
in the generated schema. Return a JSON-serializable dictionary. Returning a
dictionary with `"success": false` reports a failed step without requiring the
handler to raise.

## Adding an observation topic

`register_topic` follows the same decorator-or-direct pattern and the same
async handler, parameter, return-value, duplicate-name, `summary`, and
`overwrite` rules:

```text
register_topic(
    name,
    handler=None,
    *,
    summary="",
    overwrite=False,
)
```

For example:

```python
from typing import Any

from web_search_neo.plugins import register_topic


@register_topic("build_details", summary="Report the active plugin build.")
async def build_details(verbose: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"version": "1.0.0"}
    if verbose:
        result["features"] = ["greet"]
    return result
```

The topic appears in `web_info` automatically. Registration also re-publishes
the compact `web_info` schema with the new topic in its accepted values, so a
client can discover and call it normally:

```json
{"topic":"build_details","params":{"verbose":true}}
```

Passing `build_details` as the second argument to `register_topic` instead of
using the decorator is equivalent.

## Adding a search provider

`web_search_neo.plugins.register_search_provider(provider)` re-exports
`web_search_neo.msp_search.register_search_provider`. Pass a `SearchProvider`
instance with a valid `name`, a `search(query, num, timeout_seconds)` method,
and a `browser_url(query)` method. Search status, cooldown, caching, and
fallback routing then include it automatically.

For a small function-backed provider, the `FunctionSearchProvider` adapter is
available in `web_search_neo/msp_search.py`; use it instead of writing a full
`SearchProvider` subclass when its function and URL-template contract fits.
