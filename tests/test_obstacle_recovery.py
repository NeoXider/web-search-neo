"""Повтор шага и исправление того, что страница назвала сама (obstacle_recovery).

Тот же принцип, что у локального решения капч: пока агент не может пройти дальше,
бесплатно чинится только то, что чинится само, и каждый отказ называется словами.
Проверяется без браузера и без сети: примитивы приходят вызываемыми аргументами.
"""

from __future__ import annotations

import time

from web_search_neo import obstacle_recovery as recovery


def test_an_observing_step_retries_once_while_the_page_asks_to_wait():
    plan = recovery.retry_plan("wait", "element still loading, please wait", 1, 2)
    assert plan["retry"] is True
    assert plan["why"] == "observing_step_blocked"
    assert plan["wait_seconds"] == recovery.WAIT_STEP_SECONDS


def test_a_step_that_sends_something_is_never_repeated_on_its_own():
    for name in ("submit", "click", "fill", "scroll", "open", "captcha"):
        plan = recovery.retry_plan(name, "timed out waiting for the click", 1, 3)
        assert plan["retry"] is False, name
        assert plan["why"] == "action_changes_page_state"


def test_a_second_identical_attempt_is_refused_when_the_page_did_not_ask_to_wait():
    first = recovery.retry_plan("fetch_text", "connection reset", 1, 3)
    assert first["retry"] is True
    second = recovery.retry_plan("fetch_text", "connection reset", 2, 3)
    assert second["retry"] is False
    assert second["why"] == "same_failure_without_a_wait"


def test_a_retry_is_stopped_once_the_page_answered():
    plan = recovery.retry_plan("replay_request", "status 402 insufficient balance", 1, 3)
    assert plan["retry"] is False
    assert plan["why"] == "server_answered"


def test_the_retry_ceiling_is_named_and_holds_even_when_the_page_keeps_asking():
    plan = recovery.retry_plan("wait", "please wait", 2, 2)
    assert plan["retry"] is False
    assert plan["why"] == "retry_limit_reached"
    assert plan["attempts_used"] == 2 and plan["retry_limit"] == 2


def test_the_ceiling_is_clamped_so_no_environment_can_ask_for_a_hundred_attempts():
    assert recovery.RETRY_LIMIT_DEFAULT == 2
    assert recovery.RETRY_LIMIT_MAX == 5
    assert recovery.retry_plan("wait", "please wait", 1, 99)["retry"] is True


def test_only_the_observing_names_are_safe_to_repeat():
    for name in ("wait", "screenshot", "fetch_text", "look", "http_request"):
        assert recovery.safe_to_repeat(name) is True, name
    for name in ("submit", "click_text", "type_text", "navigate", "run_script"):
        assert recovery.safe_to_repeat(name) is False, name
    assert recovery.safe_to_repeat("") is False
    assert recovery.safe_to_repeat("WAIT") is True


def test_a_single_consent_the_page_named_is_ticked_by_its_own_label():
    answer = recovery.consent_selector([{"label": "I agree to the terms of service",
                                         "selector": "#terms"}])
    assert answer == {"fix": "consent_checkbox", "apply": True, "label": "I agree to the terms of service",
                      "selector": "#terms"}


def test_two_different_things_to_agree_with_are_left_for_the_agent():
    answer = recovery.consent_selector([{"label": "I accept the cookies", "selector": "#cookies"},
                                        {"label": "I agree to the terms", "selector": "#terms"}])
    assert answer["apply"] is False
    assert answer["why"] == "several_consent_labels"
    assert answer["labels"] == ["i accept the cookies", "i agree to the terms"]


def test_a_checkbox_nobody_asked_to_agree_with_is_left_alone():
    answer = recovery.consent_selector([{"label": "Email me about new features", "selector": "#news"}])
    assert answer["apply"] is False
    assert answer["why"] == "no_consent_word_named"
    assert answer["checked_labels"] == 1


def test_a_consent_without_a_named_control_is_refused_rather_than_guessed():
    answer = recovery.consent_selector([{"label": "I agree to the terms", "selector": ""}])
    assert answer["apply"] is False
    assert answer["why"] == "no_field_named"
    assert answer["label"] == "I agree to the terms"


def test_the_format_the_page_wrote_next_to_a_field_is_the_value_used():
    answer = recovery.format_candidates([{"label": "Expected format: +1-555-0100", "selector": "input[name=phone]"}])
    assert answer == {"fix": "field_format", "apply": True, "format": "+1-555-0100",
                      "selector": "input[name=phone]"}


def test_two_formats_for_one_field_are_a_choice_not_a_guess():
    answer = recovery.format_candidates([{"label": "Expected format: DD.MM.YYYY", "selector": "#a"},
                                         {"label": "pattern: YYYY-MM-DD", "selector": "#b"}])
    assert answer["apply"] is False
    assert answer["why"] == "several_formats_named"
    assert answer["formats"] == ["DD.MM.YYYY", "YYYY-MM-DD"]


def test_a_format_without_a_field_is_refused():
    answer = recovery.format_candidates([{"label": "Expected format: HH:mm", "selector": ""}])
    assert answer["apply"] is False
    assert answer["why"] == "no_field_named"
    assert answer["format"] == "HH:mm"


def test_a_hint_that_is_not_a_format_names_nothing():
    assert recovery.format_candidates([{"label": "Use a strong password", "selector": "#pw"}])["why"] == "no_format_named"
    assert recovery.format_candidates([])["why"] == "no_format_named"


class _Page:
    """Замена страницы: читает то, что ей подложили, и помнит каждое применённое исправление."""

    def __init__(self, states: list[dict], fails: set[str] = frozenset()) -> None:
        self.states = states
        self.fails = set(fails)
        self.reads = 0
        self.applied: list[tuple[str, object]] = []
        self.slept: list[float] = []

    def read(self) -> dict:
        state = self.states[min(self.reads, len(self.states) - 1)]
        self.reads += 1
        return state

    def apply(self, selector: str, value: object) -> dict:
        self.applied.append((selector, value))
        if selector in self.fails:
            return {"success": False, "error": "the named control is not on the page"}
        return {"success": True}

    def locate(self, needle: str) -> dict:
        return {"found": needle in (self.read().get("present") or [])}

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def test_a_wait_the_page_asked_for_is_taken_and_its_end_is_reported():
    page = _Page([{"waiting_for": "Processing your request, please wait"}, {"waiting_for": ""}])
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep)
    assert answer["fixed"] is True
    assert answer["applied"][0]["fix"] == "wait_when_asked"
    assert answer["applied"][0]["waited_seconds"] >= 0
    assert "still_waiting" not in answer["applied"][0]
    assert page.slept == [recovery.WAIT_STEP_SECONDS]
    assert page.applied == []  # подождать — не значит что-то отправить


def test_a_wait_that_did_not_end_says_so_instead_of_claiming_it_helped():
    page = _Page([{"waiting_for": "Loading, one moment"}, {"waiting_for": "Still loading, please wait"}])
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep)
    assert answer["applied"][0]["still_waiting"] is True


def test_the_consent_and_the_format_the_page_named_are_both_applied():
    page = _Page([{"waiting_for": "",
                   "consents": [{"label": "I agree to the terms", "selector": "#terms"}],
                   "format_hints": [{"label": "Expected format: DD.MM.YYYY", "selector": "#date"}]}])
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep)
    fixes = {item["fix"] for item in answer["applied"]}
    assert fixes == {"consent_checkbox", "field_format"}
    assert page.applied == [("#date", "DD.MM.YYYY"), ("#terms", True)]


def test_a_fix_the_page_refused_is_listed_with_its_reason_not_hidden():
    page = _Page([{"waiting_for": "", "consents": [{"label": "I consent", "selector": "#ok"}],
                   "format_hints": []}], fails={"#ok"})
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep, format_hint=False)
    assert answer["fixed"] is False
    assert answer["applied"] == []
    assert answer["skipped"][0]["why"] == "the named control is not on the page"


def test_an_ambiguous_page_is_left_alone_and_every_refusal_is_named():
    page = _Page([{"waiting_for": "",
                   "consents": [{"label": "I agree to the terms", "selector": "#terms"},
                                {"label": "I accept the cookies", "selector": "#cookies"}],
                   "format_hints": [{"label": "Expected format: HH:mm", "selector": ""}]}])
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep)
    assert answer["fixed"] is False and page.applied == []
    assert {item["why"] for item in answer["skipped"]} == {"several_consent_labels", "no_field_named"}


def test_the_switches_switch_each_kind_of_fix_off():
    state = {"waiting_for": "Please wait",
             "consents": [{"label": "I agree to the terms", "selector": "#terms"}],
             "format_hints": [{"label": "Expected format: HH:mm", "selector": "#t"}]}
    page = _Page([state])
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep,
                               consent=False, format_hint=False, wait=False)
    assert answer == {"fixed": False, "applied": [], "skipped": [], "note": answer["note"]}
    assert page.applied == [] and page.slept == []


def test_everything_a_fix_needs_to_act_on_arrives_from_the_page_not_from_a_guess():
    page = _Page([{"waiting_for": "", "consents": [{"label": "I accept the terms", "selector": "#terms"}],
                   "format_hints": [{"label": "Expected format: my-project", "selector": "#slug"}]}])
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep)
    written = {item["fix"]: item for item in answer["applied"]}
    assert written["field_format"]["format"] == "my-project"
    assert written["consent_checkbox"]["selector"] == "#terms"


def test_the_note_promises_only_what_the_page_named_and_nothing_more():
    page = _Page([{"waiting_for": "", "consents": [], "format_hints": []}])
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep)
    assert "guess" in answer["note"] and "skipped" in answer["note"]


def test_the_wait_is_measured_by_a_real_pause_and_not_reported_as_a_round_trip():
    # sleep приходит тем же примитивом, которым пользуется dispatch: тест подкладывает
    # настоящую паузу и сверяет, что waited_seconds — именно она, а не выдуманное число.
    page = _Page([{"waiting_for": "Processing, please wait"}, {"waiting_for": ""}])
    started = time.monotonic()
    answer = recovery.auto_fix(page.locate, page.read, page.apply, time.sleep)
    assert time.monotonic() - started >= 0.5
    assert answer["applied"][0]["waited_seconds"] >= 0.5
    assert page.slept == []


def test_prose_consent_words_map_to_the_consent_intent():
    plan = recovery.classify_prose("I agree to the use of cookies")
    assert plan["apply"] is True and plan["intent"] == "consent"
    assert "agree_to_cookies" in plan["matches"]
    assert "checkbox" in plan["needs"]


def test_prose_date_words_map_to_a_format_with_its_value():
    plan = recovery.classify_prose("Enter the date as DD.MM.YYYY")
    assert plan["apply"] is True and plan["intent"] == "field_format"
    assert plan["value"] == "DD.MM.YYYY"


def test_prose_wait_words_map_to_the_wait_intent():
    plan = recovery.classify_prose("Please wait a moment")
    assert plan["apply"] is True and plan["intent"] == "wait"


def test_prose_order_words_name_their_items_but_apply_nothing():
    plan = recovery.classify_prose("Select in order: A B C")
    assert plan["apply"] is False
    assert plan["why"] == "ordered_selection_needs_agent"
    assert plan["named"] == ["A", "B", "C"]
    bare = recovery.classify_prose("Choose the items shown above")
    assert bare["why"] == "ordered_selection_needs_agent" and bare["named"] == []


def test_prose_asking_for_two_things_is_refused_with_its_candidates():
    plan = recovery.classify_prose("Please wait a moment. I agree to the use of cookies")
    assert plan["apply"] is False
    assert plan["why"] == "ambiguous_prose"
    assert plan["candidates"] == ["consent", "wait"]
    assert recovery.classify_prose("nothing to act on here")["why"] == "no_intent_recognised"


def test_prose_consent_ticks_the_pages_single_checkbox():
    page = _Page([{"waiting_for": "", "consents": [{"label": "Subscribe", "selector": "#sub"}],
                   "format_hints": [], "prose": "I agree to the use of cookies"}])
    journal: list[dict] = []
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep, journal=journal)
    assert answer["fixed"] is True
    assert page.applied == [("#sub", True)]
    assert answer["applied"][0]["reason"].startswith("Prose ")
    assert journal == [{"fix": "consent_checkbox", "selector": "#sub", "label": "Subscribe",
                        "reversible": True, "prior": False}]


def test_prose_consent_with_several_boxes_is_left_for_the_agent():
    page = _Page([{"waiting_for": "", "consents": [{"label": "News", "selector": "#a"},
                                                  {"label": "Offers", "selector": "#b"}],
                   "format_hints": [], "prose": "I agree to the use of cookies"}])
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep)
    assert answer["fixed"] is False and page.applied == []
    assert {item["why"] for item in answer["skipped"]} == {"no_consent_word_named", "several_fields_named",
                                                           "no_format_named"}


def test_prose_format_fills_the_pages_single_field():
    page = _Page([{"waiting_for": "", "consents": [],
                   "format_hints": [{"label": "Your birth date", "selector": "#dob"}],
                   "prose": "Enter the date as DD.MM.YYYY"}])
    journal = []
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep, journal=journal)
    assert page.applied == [("#dob", "DD.MM.YYYY")]
    assert answer["applied"][0]["reason"].startswith("Prose ")
    assert journal[0]["reversible"] is False
    assert journal[0]["why"] == "field_value_before_fix_unknown"


def test_prose_wait_is_taken_when_the_live_region_is_silent():
    page = _Page([{"waiting_for": "", "prose": "Please wait a moment"},
                  {"waiting_for": "", "prose": ""}])
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep)
    assert answer["applied"][0]["fix"] == "wait_when_asked"
    assert page.slept == [recovery.WAIT_STEP_SECONDS]


def test_prose_order_and_ambiguity_are_reported_not_applied():
    for prose, why in (("Select in order: A B C", "ordered_selection_needs_agent"),
                       ("Please wait a moment. I agree to the use of cookies", "ambiguous_prose")):
        page = _Page([{"waiting_for": "", "consents": [], "format_hints": [], "prose": prose}])
        answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep)
        assert answer["fixed"] is False and page.applied == [] and page.slept == []
        assert answer["skipped"][-1]["why"] == why, prose


def test_dry_run_reports_what_would_apply_and_touches_nothing():
    page = _Page([{"waiting_for": "",
                   "consents": [{"label": "I agree to the terms", "selector": "#terms"}],
                   "format_hints": [{"label": "Expected format: DD.MM.YYYY", "selector": "#date"}],
                   "prose": ""}])
    journal: list[dict] = []
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep,
                               dry_run=True, journal=journal)
    assert answer["dry_run"] is True and answer["fixed"] is False
    assert answer["applied"] == [] and page.applied == [] and page.slept == []
    assert journal == []
    would = {item["fix"]: item for item in answer["would_apply"]}
    assert set(would) == {"consent_checkbox", "field_format"}
    assert would["consent_checkbox"]["selector"] == "#terms"
    assert would["field_format"]["format"] == "DD.MM.YYYY"
    assert all("reason" in item and "selector" in item for item in answer["would_apply"])


def test_dry_run_of_a_wait_does_not_pause():
    page = _Page([{"waiting_for": "Processing, please wait"}])
    answer = recovery.auto_fix(page.locate, page.read, page.apply, page.sleep, dry_run=True)
    assert answer["would_apply"][0]["fix"] == "wait_when_asked"
    assert page.slept == []


class _Journal:
    """Журнал применённого и примитив отката: клик снимается, заливка — нет."""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.reverted: list[tuple[str, object]] = []

    def revert(self, selector: str, prior: object) -> dict:
        self.reverted.append((selector, prior))
        if selector == "#gone":
            return {"success": False, "error": "the named control is not on the page"}
        return {"success": True, "checked": False}


def test_undo_reverses_only_the_consent_click_it_applied():
    box = _Journal()
    box.entries = [{"fix": "consent_checkbox", "selector": "#terms", "label": "I agree",
                    "reversible": True, "prior": False},
                   {"fix": "field_format", "selector": "#date", "format": "DD.MM.YYYY",
                    "reversible": False, "why": "field_value_before_fix_unknown",
                    "note": "The field's previous text was not recorded, so it cannot be restored."}]
    answer = recovery.auto_fix(lambda n: {}, lambda: {}, lambda s, v: {}, lambda s: None,
                               undo=True, journal=box.entries, revert=box.revert)
    assert answer["undone"] == [{"fix": "consent_checkbox", "selector": "#terms", "reversed": True}]
    assert box.reverted == [("#terms", False)]
    assert [item["why"] for item in answer["not_reversed"]] == ["field_value_before_fix_unknown"]
    assert [entry["fix"] for entry in box.entries] == ["field_format"]  # откаченное вычеркнуто


def test_undo_with_no_journal_says_so_instead_of_touching_the_page():
    calls: list[tuple[str, object]] = []

    def revert(selector: str, prior: object) -> dict:
        calls.append((selector, prior))
        return {"success": True}

    answer = recovery.auto_fix(lambda n: {}, lambda: {}, lambda s, v: {}, lambda s: None,
                               undo=True, journal=[], revert=revert)
    assert answer["undone"] == [] and answer["not_reversed"] == []
    assert answer["why"] == "nothing_to_undo" and calls == []


def test_undo_reports_a_control_that_is_no_longer_there():
    box = _Journal()
    box.entries = [{"fix": "consent_checkbox", "selector": "#gone", "reversible": True, "prior": False}]
    answer = recovery.auto_fix(lambda n: {}, lambda: {}, lambda s, v: {}, lambda s: None,
                               undo=True, journal=box.entries, revert=box.revert)
    assert answer["undone"] == []
    assert answer["not_reversed"][0]["why"] == "the named control is not on the page"
    assert len(box.entries) == 1  # неоткаченное остаётся в журнале


def test_undo_without_a_revert_primitive_reverses_nothing():
    entries = [{"fix": "consent_checkbox", "selector": "#terms", "reversible": True, "prior": False}]
    answer = recovery.auto_fix(lambda n: {}, lambda: {}, lambda s, v: {}, lambda s: None,
                               undo=True, journal=entries, revert=None)
    assert answer["undone"] == []
    assert answer["not_reversed"][0]["why"] == "no_revert_primitive"
    assert len(entries) == 1
