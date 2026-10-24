"""Safe expressions for macro scripts: conditions, computed values and ``{{templates}}``.

A macro script decides things - "is there a thumbnail?", "did the POST answer 200?",
"which captured request is the upload?" - and those decisions are short Python-like
expressions evaluated here against the run's variables::

    upload.ok and len(tags) > 0
    find(net.requests, method="POST", url="~/api/upload").status == 200
    "{{base}}/item/{{item.id}}"

Only a whitelist of syntax and the functions in ``FUNCTIONS`` are accepted: no
attribute access to Python objects, no imports, no lambdas, no comprehensions. An
expression from a file can read the run's data and nothing else on the machine.
Reading is forgiving the way a page script is: a missing key or index gives None
(``upload.link`` on a result without a link), so a condition can test for it, while
an unknown variable name is an error naming the known ones - that is a typo.
"""
from __future__ import annotations

import ast
import functools
import json
import re
import time
from typing import Any

MAX_EXPRESSION = 4000
MAX_SEQUENCE = 100_000  # longest string/list an expression may build ("x" * 10**9 is refused)
_TEMPLATE = re.compile(r"\{\{(.+?)\}\}", re.S)
_SIMPLE_NAME = re.compile(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*")
CONSTANTS = {"true": True, "false": False, "null": None, "none": None, "True": True, "False": False, "None": None}
# Fields holding JavaScript: a {{template}} there would be pasted into code as text, so it is
# never rendered (values travel through run_script's args instead).
RAW_FIELDS = frozenset({"script", "source"})


class ExpressionError(ValueError):
    """An expression that cannot be parsed or evaluated; the message names the expression."""


def path_get(value: Any, path: str, default: Any = None) -> Any:
    """``a.b.0.c`` into nested dicts and lists; a missing step gives *default*."""
    current = value
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)) and re.fullmatch(r"-?\d+", part):
            index = int(part)
            current = current[index] if -len(current) <= index < len(current) else None
        else:
            return default
        if current is None:
            return default
    return current


def _matches(item: Any, criteria: dict[str, Any]) -> bool:
    """Every criterion holds: equal, or a regex search when the expected text starts with ``~``."""
    for key, expected in criteria.items():
        actual = path_get(item, key)
        if isinstance(expected, str) and expected.startswith("~"):
            if actual is None or re.search(expected[1:], str(actual)) is None:
                return False
        elif actual != expected:
            return False
    return True


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        # a read's answer is often {"requests": [...]} or {"items": [...]}: search the list it holds
        for name in ("requests", "items", "results", "rows", "matches", "elements", "links", "tabs"):
            if isinstance(value.get(name), list):
                return value[name]
        lists = [item for item in value.values() if isinstance(item, list)]
        return lists[0] if len(lists) == 1 else [value]
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _find(items: Any, **criteria: Any) -> Any:
    return next((item for item in _items(items) if _matches(item, criteria)), None)


def _filter(items: Any, **criteria: Any) -> list[Any]:
    return [item for item in _items(items) if _matches(item, criteria)]


def _json(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _search(text: Any, pattern: str) -> Any:
    found = re.search(pattern, "" if text is None else str(text))
    if found is None:
        return None
    return found.group(1) if found.groups() else found.group(0)


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _range(*args: int) -> list[int]:
    values = range(*args)
    if len(values) > MAX_SEQUENCE:
        raise ExpressionError(f"range of {len(values)} items is over the {MAX_SEQUENCE} limit")
    return list(values)


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return value
    found = re.search(r"-?\d+(?:[.,]\d+)?", "" if value is None else str(value))
    if found is None:
        raise ExpressionError(f"no number in {value!r}")
    text = found.group(0).replace(",", ".")
    return float(text) if "." in text else int(text)


FUNCTIONS: dict[str, Any] = {
    # types and sizes
    "len": lambda value: 0 if value is None else len(value),
    "str": lambda value="": "" if value is None else str(value),
    "int": lambda value, base=10: int(value, base) if isinstance(value, str) else int(value),
    "float": float, "bool": bool, "number": _number,
    "type": lambda value: {type(None): "null", bool: "boolean", int: "number", float: "number", str: "string",
                           list: "array", tuple: "array", dict: "object"}.get(type(value), type(value).__name__),
    "empty": _empty, "default": lambda value, fallback: fallback if _empty(value) else value,
    # numbers
    "abs": abs, "min": min, "max": max, "round": round, "sum": sum,
    # text
    "lower": lambda text: str(text or "").lower(), "upper": lambda text: str(text or "").upper(),
    "strip": lambda text: str(text or "").strip(),
    "contains": lambda haystack, needle: haystack is not None and needle in haystack,
    "icontains": lambda haystack, needle: str(needle).lower() in str(haystack or "").lower(),
    "startswith": lambda text, prefix: str(text or "").startswith(prefix),
    "endswith": lambda text, suffix: str(text or "").endswith(suffix),
    "matches": lambda text, pattern: re.search(pattern, "" if text is None else str(text)) is not None,
    "search": _search, "replace": lambda text, old, new: str(text or "").replace(old, new),
    "split": lambda text, separator=None: str(text or "").split(separator),
    "join": lambda items, separator=", ": separator.join("" if item is None else str(item) for item in _items(items)),
    "json": _json, "to_json": lambda value: json.dumps(value, ensure_ascii=False, default=str),
    # collections
    "find": _find, "filter": _filter, "count": lambda items, **criteria: len(_filter(items, **criteria)),
    "pluck": lambda items, path: [path_get(item, path) for item in _items(items)],
    "get": path_get, "keys": lambda value: list((value or {}).keys()),
    "values": lambda value: list((value or {}).values()),
    "first": lambda items: next(iter(_items(items)), None),
    "last": lambda items: (_items(items) or [None])[-1],
    "sorted": lambda items, key=None, reverse=False: sorted(
        _items(items), key=(lambda item: path_get(item, key)) if key else None, reverse=reverse),
    "unique": lambda items: list(dict.fromkeys(_items(items))),
    "range": _range, "list": lambda value=None: _items(value),
    # time
    "now": time.time, "today": lambda: time.strftime("%Y-%m-%d"),
    "time_text": lambda fmt="%Y-%m-%d %H:%M:%S": time.strftime(fmt),
}

_BINARY = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
}
_COMPARE = {
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: b is not None and a in b, ast.NotIn: lambda a, b: b is None or a not in b,
    ast.Is: lambda a, b: a is b, ast.IsNot: lambda a, b: a is not b,
}


_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
            ast.BinOp, ast.Compare, ast.IfExp, ast.Constant, ast.Name, ast.Load, ast.Attribute,
            ast.Subscript, ast.Slice, ast.List, ast.Tuple, ast.Dict, ast.Call, ast.keyword,
            *_BINARY, *_COMPARE)
_JS_OPERATORS = (("===", "=="), ("!==", "!="), ("&&", " and "), ("||", " or "))


def _from_js(text: str) -> str:
    """``&&``, ``||``, ``!x``, ``===`` are how a page-script writer says it: accept them, outside quotes only."""
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            out.append(char)
            if char == "\\" and index + 1 < len(text):
                out.append(text[index + 1])
                index += 1
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            out.append(char)
            index += 1
            continue
        replaced = next(((js, py) for js, py in _JS_OPERATORS if text.startswith(js, index)), None)
        if replaced is not None:
            out.append(replaced[1])
            index += len(replaced[0])
        elif char == "!" and not text.startswith("!=", index):
            out.append(" not ")
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


@functools.lru_cache(maxsize=512)
def parse(expression: str) -> ast.Expression:
    """Parse once; syntax outside the whitelist is refused here, before anything runs."""
    text = str(expression).strip()
    if not text:
        raise ExpressionError("empty expression")
    if len(text) > MAX_EXPRESSION:
        raise ExpressionError(f"expression is {len(text)} characters, over the {MAX_EXPRESSION} limit")
    try:
        tree = ast.parse(_from_js(text).strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"cannot parse {expression!r}: {exc.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ExpressionError(f"{type(node).__name__} is not allowed in {expression!r}")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS):
            raise ExpressionError(f"only these functions can be called: {sorted(FUNCTIONS)} (in {expression!r})")
        if isinstance(node, ast.keyword) and node.arg is None:
            raise ExpressionError(f"**kwargs is not allowed in {expression!r}")
    return tree


def _sized(value: Any) -> Any:
    if isinstance(value, (str, list, tuple)) and len(value) > MAX_SEQUENCE:
        raise ExpressionError(f"a value of {len(value)} items is over the {MAX_SEQUENCE} limit")
    return value


def _eval(node: ast.AST, scope: dict[str, Any], source: str) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, scope, source)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in scope:
            return scope[node.id]
        if node.id in CONSTANTS:
            return CONSTANTS[node.id]
        known = sorted(name for name in scope if not name.startswith("_"))
        raise ExpressionError(f"unknown name '{node.id}' in {source!r}; known: {known}")
    if isinstance(node, ast.Attribute):
        base = _eval(node.value, scope, source)
        if isinstance(base, (list, tuple, str)) and node.attr == "length":
            return len(base)
        return path_get(base, node.attr)
    if isinstance(node, ast.Subscript):
        base = _eval(node.value, scope, source)
        if isinstance(node.slice, ast.Slice):
            parts = [None if item is None else _eval(item, scope, source)
                     for item in (node.slice.lower, node.slice.upper, node.slice.step)]
            return base[slice(*parts)] if base is not None else None
        key = _eval(node.slice, scope, source)
        try:
            return base[key]
        except (KeyError, IndexError, TypeError):
            return None
    if isinstance(node, ast.BoolOp):
        result: Any = None
        for item in node.values:
            result = _eval(item, scope, source)
            if isinstance(node.op, ast.And) and not result:
                return result
            if isinstance(node.op, ast.Or) and result:
                return result
        return result
    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, scope, source)
        return (not value) if isinstance(node.op, ast.Not) else (-value if isinstance(node.op, ast.USub) else +value)
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, scope, source), _eval(node.right, scope, source)
        if isinstance(node.op, ast.Mult) and isinstance(left, (str, list)) and isinstance(right, int):
            _sized(range(len(left) * max(right, 0)))
        return _sized(_BINARY[type(node.op)](left, right))
    if isinstance(node, ast.Compare):
        left = _eval(node.left, scope, source)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, scope, source)
            if not _COMPARE[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body if _eval(node.test, scope, source) else node.orelse, scope, source)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(item, scope, source) for item in node.elts]
    if isinstance(node, ast.Dict):
        return {_eval(key, scope, source): _eval(value, scope, source)
                for key, value in zip(node.keys, node.values) if key is not None}
    if isinstance(node, ast.Call):
        args = [_eval(item, scope, source) for item in node.args]
        kwargs = {item.arg: _eval(item.value, scope, source) for item in node.keywords}
        try:
            return _sized(FUNCTIONS[node.func.id](*args, **kwargs))
        except ExpressionError:
            raise
        except Exception as exc:
            raise ExpressionError(f"{node.func.id}(...) failed in {source!r}: {type(exc).__name__}: {exc}") from None
    raise ExpressionError(f"{type(node).__name__} is not allowed in {source!r}")


def evaluate(expression: Any, scope: dict[str, Any]) -> Any:
    """The value of one expression; a non-string (a literal from JSON) is its own value."""
    if not isinstance(expression, str):
        return expression
    try:
        return _eval(parse(expression), scope, expression)
    except ExpressionError:
        raise
    except (TypeError, ValueError, ZeroDivisionError, RecursionError) as exc:
        raise ExpressionError(f"{expression!r} failed: {type(exc).__name__}: {exc}") from None


def truthy(expression: Any, scope: dict[str, Any]) -> bool:
    return bool(evaluate(expression, scope))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def render(value: Any, scope: dict[str, Any], *, key: str | None = None) -> Any:
    """Fill every ``{{expression}}`` in a step's parameters.

    A string that is one whole template keeps the expression's type, so
    ``"times": "{{len(files)}}"`` is a number and ``"items": "{{files}}"`` a list.
    Script fields are left as written (see RAW_FIELDS); text inside ``{{ }}``
    that is not an expression at all (a Handlebars ``{{#each}}``) stays literal.
    """
    if key in RAW_FIELDS:
        return value
    if isinstance(value, str):
        whole = _TEMPLATE.fullmatch(value.strip())
        if whole is not None and _parses(whole.group(1)):
            return evaluate(whole.group(1), scope)

        def one(match: re.Match[str]) -> str:
            return _as_text(evaluate(match.group(1), scope)) if _parses(match.group(1)) else match.group(0)

        return _TEMPLATE.sub(one, value)
    if isinstance(value, dict):
        return {render(name, scope): render(item, scope, key=name if isinstance(name, str) else None)
                for name, item in value.items()}
    if isinstance(value, list):
        return [render(item, scope) for item in value]
    return value


def _parses(expression: str) -> bool:
    try:
        parse(expression)
        return True
    except ExpressionError:
        return False


def names_in(expression: Any) -> set[str]:
    """Variable names an expression reads (functions and constants left out); bad syntax gives none."""
    if not isinstance(expression, str):
        return set()
    try:
        tree = parse(expression)
    except ExpressionError:
        return set()
    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    return {node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id not in CONSTANTS and node.id not in called}


def template_names(value: Any, *, key: str | None = None) -> set[str]:
    """Variable names every ``{{template}}`` in a nested structure reads (script fields skipped)."""
    if key in RAW_FIELDS:
        return set()
    if isinstance(value, str):
        return set().union(*(names_in(match) for match in _TEMPLATE.findall(value))) if "{{" in value else set()
    if isinstance(value, dict):
        found: set[str] = set()
        for name, item in value.items():
            found |= template_names(name) | template_names(item, key=name if isinstance(name, str) else None)
        return found
    if isinstance(value, list):
        return set().union(*(template_names(item) for item in value)) if value else set()
    return set()


def has_expression_template(value: Any, *, key: str | None = None) -> bool:
    """True when a template holds more than a plain ``{{name}}`` (a path, a call, an operator)."""
    if key in RAW_FIELDS:
        return False
    if isinstance(value, str):
        return any(_SIMPLE_NAME.fullmatch(match) is None for match in _TEMPLATE.findall(value))
    if isinstance(value, dict):
        return any(has_expression_template(name) or has_expression_template(item, key=name if isinstance(name, str) else None)
                   for name, item in value.items())
    if isinstance(value, list):
        return any(has_expression_template(item) for item in value)
    return False


def check(expression: Any) -> str | None:
    """None when the expression parses, else the reason (for the validator)."""
    if not isinstance(expression, str):
        return None
    try:
        parse(expression)
        return None
    except ExpressionError as exc:
        return str(exc)
