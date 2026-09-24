"""One JavaScript semantics for run_script, execute_js and wait.script.

Three entry points used to disagree: run_script took a function body where a
top-level ``await`` was a SyntaxError, execute_js accepted the await, and
wait.script took a bare expression where ``return`` was a SyntaxError that then
waited out the whole timeout. All three now run the same way:

- the script is the body of an async function, so ``await`` works at top level
  and ``return <value>`` returns;
- a single-line script without ``return`` and without a top-level ``;`` is an
  expression and its value is returned by itself (``document.title``,
  ``await fetch(u).then(r => r.status)``);
- a SyntaxError fails at once, naming the error, instead of being retried;
- a thrown error or a rejected promise comes back as a script error.

The script runs through the driver's asynchronous script call, which both
backends support and which works inside a frame the driver has entered.
"""
from __future__ import annotations

import re
from typing import Any

# Runs the user's code in an async arrow, hands the settled value (or the
# error) to the driver's callback, and never lets a rejection escape unseen.
_WRAPPER = r"""
const __done = arguments[arguments.length - 1];
const __args = Array.prototype.slice.call(arguments, 0, -1);
(async function () { %s }).apply(window, __args).then(
  value => __done({__wsn_ok: true, value: value === undefined ? null : value,
                   undefined: value === undefined}),
  error => __done({__wsn_ok: false, name: (error && error.name) || 'Error',
                   message: String((error && error.message) || error),
                   stack: String((error && error.stack) || '').split('\n').slice(0, 4).join('\n')}));
"""
_SYNTAX = re.compile(r"SyntaxError|Unexpected token|Unexpected identifier|missing \) after", re.I)


class ScriptError(RuntimeError):
    """The page script threw, rejected, or did not compile (``syntax``)."""

    def __init__(self, message: str, *, syntax: bool = False, timed_out: bool = False):
        super().__init__(message)
        self.syntax = syntax
        self.timed_out = timed_out


_STRINGS = re.compile(r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`""")
_STATEMENTS = re.compile(
    r"(const|let|var|if|for|while|function|class|try|throw|switch|do|debugger|with|import|export)\b")


def is_expression(script: str) -> bool:
    """A one-line script with no ``return`` and no statement separator is an expression.

    Quoted text is ignored for both checks, so ``document.title === 'a;b'`` and
    ``"return"`` are expressions; ``debugger`` and ``with`` are statements.
    """
    text = script.strip().rstrip(";").strip()
    if not text or "\n" in text:
        return False
    code = _STRINGS.sub('""', text)
    if ";" in code:
        return False
    return re.search(r"(^|[^\w$.])return\b", code) is None and not _STATEMENTS.match(code)


def body_for(script: str) -> str:
    """The async-function body the wrapper runs for ``script``."""
    if is_expression(script):
        return f"return ({script.strip().rstrip(';')}\n);"
    return f"{script}\n"


def run(driver: Any, script: str, args: list[Any] | None = None) -> tuple[Any, bool]:
    """``(value, returned_undefined)``; raises :class:`ScriptError` on a page error."""
    try:
        outcome = driver.execute_async_script(_WRAPPER % body_for(script), *(args or []))
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        if _SYNTAX.search(text):
            detail = str(exc).replace("javascript error:", "").strip().splitlines()[0][:300]
            raise ScriptError(f"SyntaxError in the script: {detail}", syntax=True) from exc
        raise
    if not isinstance(outcome, dict) or "__wsn_ok" not in outcome:
        return outcome, False
    if not outcome.get("__wsn_ok"):
        # The wrapper compiled and ran, so this was thrown at run time: even a
        # SyntaxError (``JSON.parse("x")``) is then an ordinary script error, not
        # a script that did not compile - only the except branch above is that.
        name = str(outcome.get("name") or "Error")
        message = f"{name}: {outcome.get('message')}"
        stack = str(outcome.get("stack") or "")
        raise ScriptError(message + (f"\n{stack}" if stack and stack not in message else ""))
    return outcome.get("value"), bool(outcome.get("undefined"))
