"""Canonical action discriminators and bounded client-facing diagnostics."""
from __future__ import annotations

import difflib
from typing import Any, Collection

ACTION_KEY_ALIASES = ("type", "name", "tool", "command", "op", "operation", "method")


def normalized_name(value: Any, aliases: dict[str, str]) -> str:
    """Only strings are action names; JSON null must not become action 'none'."""
    name = value.strip().lower() if isinstance(value, str) else ""
    return aliases.get(name, name)


def received_keys(arguments: dict[str, Any]) -> str:
    """Report shape without echoing URLs, credentials, bodies or other values."""
    keys = sorted(str(key)[:80] for key in arguments)
    shown = repr(keys[:20])
    return shown + (f" (+{len(keys) - 20} more keys)" if len(keys) > 20 else "")


def _selector_value(value: Any) -> str:
    """Bound selector values; invalid containers may hold arbitrary secrets."""
    if isinstance(value, str):
        return repr(value[:80])
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)[:80]
    return f"<{type(value).__name__}>"


def unsupported_action_error(action_name: str, arguments: dict[str, Any],
                             available: Collection[str]) -> str:
    """Give one recovery path rather than repeating the entire action registry."""
    if not action_name:
        return (
            'Every action object needs an "action" key with a non-empty string. '
            f"Received keys: {received_keys(arguments)}. "
            'Pass actions as a JSON array of objects, e.g. '
            '{"actions":[{"action":"fetch_text","url":"https://example.com"}]}. '
            'An empty object or missing/empty action may mean the client pruned keys '
            'against its schema: refresh web_action and ensure actions.items declares '
            'items.properties.action and required=["action"]. '
            "Use web_info(topic='actions') for names and "
            "web_info(topic='action_schema', params={'action':'fetch_text'}) for parameters. "
            'type/name/tool/command/op/operation/method aliases are accepted; action is canonical.'
        )
    close = difflib.get_close_matches(action_name, available, n=3, cutoff=0.6)
    suggestion = f" Did you mean {close}?" if close else ""
    return (
        f"Unsupported action: {action_name[:100]}.{suggestion} "
        f"Received keys: {received_keys(arguments)}. "
        "Use web_info(topic='actions') to find the exact action name, then "
        "web_info(topic='action_schema', params={'action':'<action>'}) for its parameters."
    )


def resolve_action_call(facade: Any, raw: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """Resolve discriminators without consuming a real parameter called name/op/method.

    With no canonical key, prefer aliases naming a registered action; the
    chosen action's actual model decides which other keys are parameters. A
    conflict is refused before a handler can produce side effects.
    """
    arguments = dict(raw)
    aliases = facade._ACTION_NAME_ALIASES
    candidates = [key for key in ACTION_KEY_ALIASES if normalized_name(arguments.get(key), aliases)]
    recognized = [key for key in candidates if normalized_name(arguments[key], aliases) in facade._ACTIONS]
    source = recognized[0] if recognized else candidates[0] if candidates else None
    if "action" in arguments:
        source = "action"
    else:
        # op='show' can be a real macro parameter even though 'show' is also
        # an action. Prefer a candidate whose model explains the other keys.
        viable = []
        for key in recognized:
            name = normalized_name(arguments[key], aliases)
            fields = facade._argument_model(facade._ACTIONS[name].tool_name).model_fields
            if all(
                other == key or other in fields or normalized_name(arguments[other], aliases) == name
                for other in ACTION_KEY_ALIASES if other in arguments
            ):
                viable.append(key)
        if len({normalized_name(arguments[key], aliases) for key in viable}) > 1:
            values = ", ".join(f"{key}={_selector_value(raw[key])}" for key in viable)
            return "", arguments, (
                f"Conflicting action discriminators: {values}. Received keys: {received_keys(raw)}. "
                'Aliases ambiguously select multiple actions; set canonical "action" explicitly '
                'and use the selected action schema for its other parameters.'
            )
        if viable:
            source = viable[0]
    action_name = normalized_name(arguments.get(source), aliases) if source else ""
    if not action_name:
        return "", arguments, facade._unsupported_action_error("", raw)
    spec = facade._ACTIONS.get(action_name)
    fields = facade._argument_model(spec.tool_name).model_fields if spec is not None else {}
    discriminators = {
        key: normalized_name(value, aliases)
        for key, value in raw.items()
        if key == source or key in ACTION_KEY_ALIASES and key not in fields
    }
    conflicts = {key: value for key, value in discriminators.items() if value != action_name}
    if conflicts:
        shown = {source: action_name, **conflicts}
        values = ", ".join(f"{key}={_selector_value(raw[key])}" for key in shown)
        return action_name, arguments, (
            f"Conflicting action discriminators: {values}. Received keys: {received_keys(raw)}. "
            'Keep the canonical "action" and remove or match conflicting aliases. '
            'Actual action parameters named name/op/method remain parameters.'
        )
    arguments.pop("action", None)
    for key in discriminators:
        arguments.pop(key, None)
    return action_name, arguments, None
