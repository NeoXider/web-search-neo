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

# Просьба страницы своими словами: каждый шаблон — одно намерение и то, что
# ему нужно. Таблица явная и детерминированная: совпали два разных намерения —
# отказ, а не догадка. Для field_format первая непустая группа — само значение,
# иначе значением считается всё совпадение.
INTENT_VOCABULARY = (
    {"match": "please_wait", "intent": "wait",
     "pattern": r"please wait(?: a moment)?|just a moment|one moment please|wait a moment",
     "needs": "a live region asking to wait"},
    {"match": "agree_to_cookies", "intent": "consent",
     "pattern": r"i agree to the use of cookies|i agree to cookies|accept (?:the |all )?cookies|"
                r"agree to (?:the use of )?cookies",
     "needs": "the page's single unchecked checkbox"},
    {"match": "agree_to_terms", "intent": "consent",
     "pattern": r"\bi agree to the (?:terms|use)\b|\bi accept the terms\b",
     "needs": "the page's single unchecked checkbox"},
    {"match": "date_format_words", "intent": "field_format",
     "pattern": r"enter the date as\s+(\S{2,40})|\bdate as\s+(\S{2,40})",
     "needs": "the field the format is written beside"},
    {"match": "select_in_order", "intent": "ordered_selection",
     "pattern": r"select in order|choose the items shown above|tap .* in (?:the )?order|select .* in order",
     "needs": "you tapping the named items in order"},
)

_ORDERED_TAIL = re.compile(r"select in order\s*:?\s*(.+)", re.I | re.S)

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


def _ordered_items(text: str) -> list[str]:
    """Имена после «Select in order: …»: запятые, точки с запятой или пробелы."""
    match = _ORDERED_TAIL.search(str(text or ""))
    if not match:
        return []
    tail = match.group(1).strip()[:400]
    parts = re.split(r"[,;\n]+", tail)
    if len(parts) == 1:
        parts = tail.split()
    return [part.strip().strip("'\"") for part in parts if part.strip()][:12]


def classify_prose(text: str) -> dict[str, Any]:
    """Просьба страницы своими словами: один текст — одно намерение.

    Два разных намерения на один текст — отказ ``ambiguous_prose`` с кандидатами.
    Порядок, который страница назвала («Select in order: A B C»), распознаётся,
    но не применяется: нажимать по порядку — решение агента (``ordered_selection``).
    """
    found: list[tuple[dict[str, str], re.Match[str]]] = []
    for entry in INTENT_VOCABULARY:
        match = re.search(entry["pattern"], str(text or ""), re.I)
        if match:
            found.append((entry, match))
    if not found:
        return {"fix": "prose_intent", "apply": False, "why": "no_intent_recognised"}
    intents = sorted({entry["intent"] for entry, _ in found})
    if len(intents) > 1:
        return {"fix": "prose_intent", "apply": False, "why": "ambiguous_prose",
                "candidates": intents, "matches": [entry["match"] for entry, _ in found],
                "note": ("The page's own words ask for two different things; "
                         "which one is meant is your call.")}
    intent = intents[0]
    matches = [entry["match"] for entry, _ in found]
    if intent == "ordered_selection":
        return {"fix": "ordered_selection", "apply": False, "why": "ordered_selection_needs_agent",
                "named": _ordered_items(text), "matches": matches,
                "note": ("The page names items to choose in order; tapping them in that "
                         "order is your call, this tool never clicks through a choice.")}
    plan: dict[str, Any] = {"fix": "prose_intent", "apply": True, "intent": intent,
                            "matches": matches,
                            "needs": next(e["needs"] for e, _ in found if e["intent"] == intent)}
    if intent == "field_format":
        _, match = found[0]
        groups = match.groups() if match.groups() else (match.group(0),)
        plan["value"] = next((part for part in groups if part), match.group(0))
    return plan


def _undo_applied(journal: list[dict[str, Any]] | None,
                  revert: Callable[[str, Any], dict[str, Any]] | None,
                  *, limit: int = 3) -> dict[str, Any]:
    """Откатить только то, что применил сам auto_fix, в обратном порядке.

    Откатывается клик по чекбоксу согласия (в журнале ``reversible: True``).
    Значение поля не восстанавливается — прежний текст не записывался, —
    а отправленная форма не откатывается вовсе: об этом говорится прямо.
    """
    undone: list[dict[str, Any]] = []
    not_reversed: list[dict[str, Any]] = []
    entries = list(reversed(journal or []))
    if not entries:
        return {"fixed": False, "applied": [], "skipped": [], "undone": [], "not_reversed": [],
                "why": "nothing_to_undo",
                "note": "This tool applied nothing to reverse."}
    finished: set[int] = set()
    for entry in entries:
        selector = entry.get("selector")
        if not entry.get("reversible"):
            not_reversed.append({"fix": entry.get("fix"), "selector": selector,
                                 "why": str(entry.get("why") or "not_reversible"),
                                 "note": str(entry.get("note") or
                                             "This fix cannot be restored.")})
            continue
        if revert is None:
            not_reversed.append({"fix": entry.get("fix"), "selector": selector,
                                 "why": "no_revert_primitive",
                                 "note": "No page primitive was given to reverse the click."})
            continue
        result = revert(str(selector), entry.get("prior", False)) or {}
        if result.get("success"):
            finished.add(id(entry))
            undone.append({"fix": entry.get("fix"), "selector": selector, "reversed": True})
        else:
            not_reversed.append({"fix": entry.get("fix"), "selector": selector,
                                 "why": str(result.get("error") or "rejected")})
    if journal is not None:
        journal[:] = [entry for entry in journal if id(entry) not in finished]
    return {"fixed": False, "applied": [], "skipped": [],
            "undone": undone[:limit], "not_reversed": not_reversed[:limit],
            "note": ("Only what this tool itself applied was touched. A consent click is "
                     "reversed; a filled field keeps its text and a submitted form is "
                     "not reversible — this tool never submits one.")}


def auto_fix(locate: Callable[[str], dict[str, Any]], read: Callable[[], dict[str, Any]],
             apply: Callable[[str, Any], dict[str, Any]], sleep: Callable[[float], None],
             *, consent: bool = True, format_hint: bool = True, wait: bool = True,
             limit: int = 3, dry_run: bool = False, undo: bool = False,
             journal: list[dict[str, Any]] | None = None,
             revert: Callable[[str, Any], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Чинит препятствие, которое страница назвала сама; описывает каждое действие.

    ``read()`` читает состояние страницы (``{"waiting_for": ..., "consents": [...],
    "format_hints": [...], "prose": ...}``), ``apply(selector, value)`` применяет
    исправление, ``locate(selector)`` проверяет цель. Каждое применённое видно в
    ``applied``, каждое пропущенное — с причиной в ``skipped``.

    ``dry_run=True`` ничего не трогает: то, что было бы применено, лежит в
    ``would_apply`` (цели, адреса, причины). ``undo=True`` откатывает только то,
    что этот вызов записал в ``journal`` (через ``revert(selector, prior)``);
    журнал ведёт вызывающий, обычно по одному на сессию.
    """
    if undo:
        return _undo_applied(journal, revert, limit=limit)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    planned_ops: list[dict[str, Any]] = []
    state = read() or {}
    prose = classify_prose(state.get("prose") or "")

    def prose_for(intent: str, enabled: bool) -> dict[str, Any] | None:
        if enabled and prose.get("apply") and prose.get("intent") == intent:
            return prose
        return None

    waiting = str(state.get("waiting_for") or "")
    wait_reason = "The page's live region asked to wait." if is_waiting_for_time(waiting) else ""
    said_wait = prose_for("wait", wait)
    if not wait_reason and said_wait is not None:
        wait_reason = f"Prose {said_wait['matches'][0]!r} asks to wait."
    if wait and wait_reason:
        if dry_run:
            planned_ops.append({"fix": "wait_when_asked", "wait_seconds": WAIT_STEP_SECONDS,
                                "reason": wait_reason})
        else:
            started = time.monotonic()
            sleep(WAIT_STEP_SECONDS)
            waited = round(time.monotonic() - started, 2)
            after = read() or {}
            entry = {"fix": "wait_when_asked", "waited_seconds": waited, "reason": wait_reason}
            if is_waiting_for_time(str(after.get("waiting_for") or "")):
                entry["still_waiting"] = True
            applied.append(entry)

    planned = format_candidates(state.get("format_hints")) if format_hint else None
    if planned and planned.get("apply"):
        planned_ops.append({"fix": "field_format", "selector": planned["selector"],
                            "format": planned["format"], "value": planned["format"],
                            "reason": "The page wrote this format beside the field."})
    elif planned:
        skipped.append(planned)
        if planned.get("why") == "no_format_named":
            said_format = prose_for("field_format", format_hint)
            if said_format is not None:
                fields = [(label, sel) for label, sel in _pairs(state.get("format_hints")) if sel]
                value = str(said_format["value"])
                if len(fields) == 1:
                    planned_ops.append({"fix": "field_format", "selector": fields[0][1],
                                        "format": value, "value": value,
                                        "reason": (f"Prose {said_format['matches'][0]!r} names this "
                                                   "format for the page's only field.")})
                elif not fields:
                    skipped.append({"fix": "field_format", "apply": False, "why": "no_field_named",
                                    "format": value,
                                    "note": "Prose names a format but the page shows no field for it."})
                else:
                    skipped.append({"fix": "field_format", "apply": False,
                                    "why": "several_fields_named", "format": value,
                                    "note": ("Prose names one format but the page shows several fields; "
                                             "which one it is for is your call.")})

    planned = consent_selector(state.get("consents")) if consent else None
    if planned and planned.get("apply"):
        planned_ops.append({"fix": "consent_checkbox", "selector": planned["selector"],
                            "label": planned["label"], "value": True,
                            "reason": "The control's own label asks to agree."})
    elif planned:
        skipped.append(planned)
        if planned.get("why") == "no_consent_word_named":
            said_consent = prose_for("consent", consent)
            if said_consent is not None:
                named = [(str(c or "").strip(), str(s or "").strip())
                         for c, s in _pairs(state.get("consents")) if s]
                if len(named) == 1:
                    planned_ops.append({"fix": "consent_checkbox", "selector": named[0][1],
                                        "label": named[0][0], "value": True,
                                        "reason": (f"Prose {said_consent['matches'][0]!r} asks to agree "
                                                   "and the page shows a single checkbox.")})
                elif not named:
                    skipped.append({"fix": "consent_checkbox", "apply": False, "why": "no_field_named",
                                    "note": "Prose asks to agree but the page shows no checkbox for it."})
                else:
                    skipped.append({"fix": "consent_checkbox", "apply": False,
                                    "why": "several_fields_named",
                                    "note": ("Prose asks to agree but the page shows several checkboxes; "
                                             "which one is meant is your call.")})

    if (consent or format_hint or wait) and not prose.get("apply") \
            and prose.get("why") in {"ambiguous_prose", "ordered_selection_needs_agent"}:
        skipped.append(prose)

    if dry_run:
        return {"fixed": False, "dry_run": True, "applied": [], "would_apply": planned_ops[:limit],
                "skipped": skipped[:limit],
                "note": ("Nothing on the page was touched. would_apply lists exactly what a real "
                         "run would do: targets, selectors and reasons.")}

    for op in planned_ops:
        result = apply(op["selector"], op["value"]) or {}
        entry = {"fix": op["fix"], "selector": op["selector"], "reason": op["reason"]}
        entry.update({"label": op["label"]} if "label" in op else {"format": op["format"]})
        if not result.get("success"):
            entry = {"fix": op["fix"], "selector": op["selector"],
                     "why": str(result.get("error") or "rejected")}
            entry.update({"label": op["label"]} if "label" in op else {"format": op["format"]})
        (applied if result.get("success") else skipped).append(entry)
        if result.get("success") and journal is not None:
            if op["fix"] == "consent_checkbox":
                journal.append({"fix": "consent_checkbox", "selector": op["selector"],
                                "label": op.get("label"), "reversible": True, "prior": False})
            elif op["fix"] == "field_format":
                journal.append({"fix": "field_format", "selector": op["selector"],
                                "format": op.get("format"), "reversible": False,
                                "why": "field_value_before_fix_unknown",
                                "note": ("The field's previous text was not recorded, "
                                         "so it cannot be restored.")})

    return {"fixed": bool(applied), "applied": applied[:limit], "skipped": skipped[:limit],
            "note": ("Only what the page itself named was changed. A fix that needed a guess was "
                     "skipped and says so.")}
