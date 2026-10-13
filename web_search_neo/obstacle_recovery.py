"""Что страница сама назвала как своё препятствие и что с этим можно сделать.

Тот же принцип, что у локального решения слайдерных капч: пока агент *не может
продвинуться*, бесплатно чинится то, что чинится само, и ответ честно говорит,
что именно было сделано. Когда страница отвечает — трогать её не нужно.

Два уровня:

* ``retry_plan`` — повторять ли шаг ещё раз. Повторяется только действие, которое
  ничего не меняет (посмотреть, подождать, прочитать): submit, click и запись в
  поле сами по себе не повторяются — повторить отправку формы без ведома агента
  значит отправить её дважды.
* ``auto_fix`` — чинит то, что страница сказала сама: несогласие, которое она
  требует подтвердить, формат поля, который она написала рядом с полем,
  «подожди немного». Каждое исправление описано в ответе; сомнительное не
  делается вовсе.

Ничего не угадывается по именам: поле берётся то, что назвала страница, значение —
то, которое она подсказала. Браузерные примитивы приходят вызываемыми аргументами,
поэтому этот модуль не знает ни о Selenium, ни о сессиях и проверяется без сети.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

# Повторяются только наблюдающие шаги. Записывающие не повторяются никогда:
# повторить отправку формы — это вторая отправка, а не вторая попытка посмотреть.
OBSERVING_ACTIONS = frozenset({"wait", "wait_frames", "wait_challenge", "screenshot", "show", "look",
                               "tabs", "fetch_text", "fetch_links", "fetch_many", "search",
                               "http_request", "replay_request"})

# Шаг, который что-то отправил или изменил. Его повтор — отдельное решение,
# не автоматическое: он может отправить форму второй раз.
TERMINAL_ACTIONS = frozenset({"submit", "click", "click_text", "fill", "type_text", "input", "press_keys",
                              "pointer", "touch", "scroll", "upload", "open", "open_many", "navigate",
                              "reload", "cookies", "local_storage", "run_script", "inject_script",
                              "mock", "dialogs", "context", "captcha"})

# Действие уже дошло до сервера: страница ответила, чинить нечего.
_SERVER_ANSWERED = re.compile(r"status[^\d]{0,8}\b(2\d\d|3\d\d|4\d\d|5\d\d)\b", re.I)
_WAIT_PHRASES = re.compile(r"please wait|try again (?:in|later|shortly)|loading|processing|"
                           r"just a moment|one moment|not ready yet|hold on", re.I)
_CONSENT_WORDS = re.compile(r"\b(i agree|i accept|agree to|accept the|i consent|consent|terms|"
                            r"terms of service|privacy policy|i am not a robot|human)\b", re.I)
_FORMAT_HINT = re.compile(r"(?:expected|use|enter|must match)\s+(?:the\s+)?(?:format|pattern)"
                          r"\s*(?:of|is|:|=)\s*(\S{2,60})|(?:format|pattern)\s*[:=]\s*(\S{2,60})", re.I)

RETRY_LIMIT_DEFAULT = 2
RETRY_LIMIT_MAX = 5
WAIT_STEP_SECONDS = 2.0


def already_reached_server(error: str) -> bool:
    """Шаг получил ответ сервера: препятствие не в транспорте и повторять не за чем."""
    return bool(_SERVER_ANSWERED.search(str(error or "")))


def is_waiting_for_time(error: str) -> bool:
    """Страница сама просит подождать: это условие, а не отказ."""
    return bool(_WAIT_PHRASES.search(str(error or "")))


def safe_to_repeat(action_name: str) -> bool:
    """Только наблюдающий шаг можно повторить без ведома агента."""
    name = str(action_name or "").strip().lower()
    return name in OBSERVING_ACTIONS and name not in TERMINAL_ACTIONS


def retry_plan(action_name: str, error: str, attempts: int, limit: int) -> dict[str, Any]:
    """Повторять ли шаг ещё раз и почему. Отказ называется словами."""
    plan: dict[str, Any] = {"action": action_name, "attempts_used": attempts, "retry_limit": limit}
    if attempts >= limit:
        return {**plan, "retry": False, "why": "retry_limit_reached"}
    if not safe_to_repeat(action_name):
        return {**plan, "retry": False, "why": "action_changes_page_state",
                "note": ("This step writes or sends something. Repeating it would do it again, "
                         "so it is retried only when you ask for it explicitly.")}
    if already_reached_server(error):
        return {**plan, "retry": False, "why": "server_answered",
                "note": "The page answered this call; repeating the same observation proves nothing."}
    if attempts > 1 and not is_waiting_for_time(error):
        return {**plan, "retry": False, "why": "same_failure_without_a_wait",
                "note": "The page did not ask to wait, so a second identical attempt would fail the same way."}
    return {**plan, "retry": True, "why": "observing_step_blocked",
            "wait_seconds": WAIT_STEP_SECONDS}


def _pairs(items: Any) -> list[tuple[Any, Any]]:
    """Подсказки страницы приходят и парами, и объектами `{"label","selector"}`."""
    pairs: list[tuple[Any, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            pairs.append((item.get("label") or item.get("text"), item.get("selector")))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append((item[0], item[1]))
        else:
            pairs.append((item, None))
    return pairs


def consent_selector(captions: Any) -> dict[str, Any]:
    """Единственный чекбокс согласия: по подписи, которую назвала сама страница.

    Один кандидат — одно исправление. Два разных текста согласия — уже выбор,
    который должен сделать агент, а не догадка о том, какой чекбокс «настоящий».
    """
    named = [(str(caption or "").strip(), str(selector or "").strip())
             for caption, selector in _pairs(captions)]
    matches = [(caption, selector) for caption, selector in named if caption and _CONSENT_WORDS.search(caption)]
    if not matches:
        return {"fix": "consent_checkbox", "apply": False, "why": "no_consent_word_named",
                "checked_labels": len(named)}
    distinct = sorted({re.sub(r"\s+", " ", caption).lower() for caption, _ in matches})
    if len(distinct) > 1:
        return {"fix": "consent_checkbox", "apply": False, "why": "several_consent_labels",
                "labels": distinct[:4],
                "note": "The page offers more than one thing to agree to; which one is meant is your call."}
    caption, selector = matches[0]
    if not selector:
        return {"fix": "consent_checkbox", "apply": False, "why": "no_field_named", "label": caption,
                "note": "The page asks for agreement but its control was not named."}
    return {"fix": "consent_checkbox", "apply": True, "label": caption, "selector": selector}


def format_candidates(hints: Any) -> dict[str, Any]:
    """Формат, который подсказала страница: одно значение — одно исправление."""
    values: list[tuple[str, str]] = []
    for label, selector in _pairs(hints):
        match = _FORMAT_HINT.search(str(label or ""))
        if match:
            values.append((next(part for part in match.groups() if part), str(selector or "")))
    distinct = sorted({value for value, _ in values if value})
    if not distinct:
        return {"fix": "field_format", "apply": False, "why": "no_format_named"}
    if len(distinct) > 1:
        return {"fix": "field_format", "apply": False, "why": "several_formats_named",
                "formats": distinct[:4],
                "note": "The page names more than one format for this field; picking one is your call."}
    selector = next((sel for value, sel in values if value == distinct[0] and sel), "")
    if not selector:
        return {"fix": "field_format", "apply": False, "why": "no_field_named", "format": distinct[0],
                "note": "The page named a format but not which field it is for."}
    return {"fix": "field_format", "apply": True, "format": distinct[0], "selector": selector}


def auto_fix(locate: Callable[[str], dict[str, Any]], read: Callable[[], dict[str, Any]],
             apply: Callable[[str, Any], dict[str, Any]], sleep: Callable[[float], None],
             *, consent: bool = True, format_hint: bool = True, wait: bool = True,
             limit: int = 3) -> dict[str, Any]:
    """Чинит препятствие, которое страница назвала сама; описывает каждое действие.

    ``read()`` читает состояние страницы (``{"waiting_for": ..., "consents": [...],
    "format_hints": [...]}``), ``apply(selector, value)`` применяет исправление,
    ``locate(selector)`` проверяет цель. Каждое применённое видно в ``applied``,
    каждое пропущенное — с причиной в ``skipped``.
    """
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    state = read() or {}

    if wait and is_waiting_for_time(str(state.get("waiting_for") or "")):
        started = time.monotonic()
        sleep(WAIT_STEP_SECONDS)
        waited = round(time.monotonic() - started, 2)
        after = read() or {}
        entry = {"fix": "wait_when_asked", "waited_seconds": waited}
        if is_waiting_for_time(str(after.get("waiting_for") or "")):
            entry["still_waiting"] = True
        applied.append(entry)

    planned = format_candidates(state.get("format_hints")) if format_hint else None
    if planned and planned.get("apply"):
        selector, value = planned["selector"], planned["format"]
        result = apply(selector, value) or {}
        entry = {"fix": "field_format", "selector": selector, "format": value}
        (applied if result.get("success") else skipped).append(
            entry if result.get("success") else {**entry, "why": str(result.get("error") or "rejected")})
    elif planned:
        skipped.append(planned)

    planned = consent_selector(state.get("consents")) if consent else None
    if planned and planned.get("apply"):
        selector, label = planned["selector"], planned["label"]
        result = apply(selector, True) or {}
        entry = {"fix": "consent_checkbox", "selector": selector, "label": label}
        (applied if result.get("success") else skipped).append(
            entry if result.get("success") else {**entry, "why": str(result.get("error") or "rejected")})
    elif planned:
        skipped.append(planned)

    return {"fixed": bool(applied), "applied": applied[:limit], "skipped": skipped[:limit],
            "note": ("Only what the page itself named was changed. A fix that needed a guess was "
                     "skipped and says so.")}
