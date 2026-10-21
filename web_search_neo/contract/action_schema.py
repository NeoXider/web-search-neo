"""Build the common action envelope from real registered argument schemas.

The envelope advertises every parameter to schema-pruning clients. Each action
still validates its own required keys and types at dispatch time: the union is
discovery metadata, not a replacement for those individual contracts.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable


def _namespace_refs(value: Any, names: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [_namespace_refs(item, names) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _namespace_refs(item, names) for key, item in value.items()}
    reference = result.get("$ref", "")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        key, separator, tail = reference[len("#/$defs/"):].partition("/")
        decoded = key.replace("~1", "/").replace("~0", "~")
        if decoded in names:
            target = names[decoded].replace("~", "~0").replace("/", "~1")
            result["$ref"] = f"#/$defs/{target}" + (separator + tail if separator else "")
    return result


def action_envelope(schemas: Iterable[tuple[str, dict[str, Any]]],
                    aliases: Iterable[str] = ()) -> dict[str, Any]:
    """Copy and union properties, namespacing definitions to prevent collisions."""
    variants: dict[str, list[dict[str, Any]]] = {}
    definitions: dict[str, Any] = {}
    for index, (action, original) in enumerate(schemas):
        schema = deepcopy(original)
        names = {key: f"action_{index}_{action}_{key}" for key in schema.get("$defs", {})}
        schema = _namespace_refs(schema, names)
        definitions.update({names[key]: value for key, value in schema.get("$defs", {}).items()})
        for key, property_schema in schema.get("properties", {}).items():
            choices = variants.setdefault(key, [])
            if property_schema not in choices:
                choices.append(property_schema)
    # summary is a dispatcher parameter, so it has no handler signature.
    variants.setdefault("summary", []).append({"type": "string", "enum": ["full", "min"]})
    for alias in aliases:
        variants.setdefault(alias, []).append({"type": "string"})
    properties = {
        key: choices[0] if len(choices) == 1 else {"anyOf": choices}
        for key, choices in variants.items()
    }
    properties["action"] = {
        "type": "string",
        "description": "Required canonical action name from web_info(topic='actions').",
    }
    result: dict[str, Any] = {
        "type": "object", "properties": properties, "required": ["action"], "additionalProperties": True,
    }
    if definitions:
        result["$defs"] = definitions
    return result
