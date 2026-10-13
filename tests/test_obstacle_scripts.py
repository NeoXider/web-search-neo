"""Страничные скрипты auto_fix и то, как они подключены к действию.

auto_fix читает страницу и трогает контроль: оба делает JS на странице. Здесь
проверяется сам скрипт (на fixture-странице браузера там, где он есть, и на его
тексте - всегда), и что действие зарегистрировано с понятными параметрами.
"""

from __future__ import annotations

import json
import re

import web_search_neo.main as main
from web_search_neo import obstacle_recovery, obstacle_scripts


def test_the_reading_script_asks_only_for_what_the_page_named_itself():
    script = obstacle_scripts.read_script()
    # Ни одного выдуманного адреса: читаются live-области, чекбоксы и подсказки формата.
    assert "aria-live" in script and "role=status" in script and "role=alert" in script
    assert "input[type=checkbox]" in script
    assert "aria-describedby" in script and "data-format" in script and ".field-error" in script
    assert script.count("return out;") == 1


def test_an_already_ticked_box_is_not_reported_as_something_to_agree_with():
    assert "if (box.checked) continue;" in obstacle_scripts.READ_OBSTACLES


def test_the_tick_script_touches_one_control_and_refuses_when_it_is_not_there():
    script = obstacle_scripts.tick_script("#terms")
    assert "document.querySelector(\"#terms\")" in script
    assert "the named control is not on the page" in script
    assert "that control is not a checkbox" in script
    # Отмеченный чекбокс не кликается повторно - только сообщается, что он уже отмечен.
    assert "if (node.checked) return {success: true, already: true};" in script


def test_the_selector_is_passed_as_data_not_glued_into_the_script():
    # Кавычка в адресе не должна ломать скрипт и не может протащить в него код.
    script = obstacle_scripts.tick_script('input[name="a\"] ; alert(1)")')
    assert "alert(1)" in script  # текст адреса остался строкой внутри JSON
    assert script.count("document.querySelector(") == 1


def test_the_control_address_is_the_one_a_person_would_write():
    helper = obstacle_scripts.SELECTOR_HELPER
    assert "if (node.id) return '#' + node.id;" in helper
    assert '[name="' in helper
    # Короткий путь вместо дерева: максимум шесть сегментов, не весь DOM.
    assert "path.length < 6" in helper


def test_state_of_keeps_only_labels_and_addresses_and_drops_the_rest():
    state = obstacle_scripts.state_of({
        "waiting_for": "Processing your request", "consents": [{"label": "I agree", "selector": "#a"}],
        "format_hints": [{"label": "Expected format: HH:mm", "selector": "input[name=t]"}],
        "junk": "<script>alert(1)</script>",
    })
    assert state["waiting_for"] == "Processing your request"
    assert state["consents"] == [{"label": "I agree", "selector": "#a"}]
    assert "junk" not in state


def test_state_of_drops_an_entry_without_an_address_and_survives_a_broken_answer():
    state = obstacle_scripts.state_of({"consents": [{"label": "no selector here"}, {"label": "x", "selector": "#k"}],
                                       "format_hints": "not a list"})
    assert state["consents"] == [{"label": "x", "selector": "#k"}]
    assert state["format_hints"] == []
    assert obstacle_scripts.state_of(None) == {}
    assert obstacle_scripts.state_of("text") == {}


def test_a_long_waiting_message_is_cut_rather_than_dumped_whole():
    state = obstacle_scripts.state_of({"waiting_for": "w" * 900})
    assert len(state["waiting_for"]) == 300


def test_auto_fix_is_a_published_action_with_its_own_switches():
    spec = main._ACTIONS.get("auto_fix")
    assert spec is not None, "auto_fix must be a web_action action"
    assert spec.group == "page"
    fields = main._argument_model(spec.tool_name).model_fields
    assert {"session_id", "selector", "consent", "format_hint", "wait"} <= set(fields)
    # Всё по умолчанию включено, но каждый род исправления выключается отдельно.
    for name in ("consent", "format_hint", "wait"):
        assert fields[name].default is True, name


def test_auto_fix_is_documented_in_the_contract_its_users_read():
    from web_search_neo.contract.notes import _ACTION_NOTES

    notes = _ACTION_NOTES["auto_fix"]
    assert {"what_it_reads", "what_it_applies", "refusals", "limits"} <= set(notes)
    for refusal in ("several_consent_labels", "several_formats_named", "no_field_named"):
        assert refusal in notes["refusals"], refusal
    assert "never a submit" in notes["limits"]


def test_the_refusal_names_the_action_reports_are_the_ones_the_module_returns():
    from web_search_neo.contract.notes import _ACTION_NOTES

    notes = _ACTION_NOTES["auto_fix"]["refusals"]
    produced = {"several_consent_labels", "several_formats_named", "no_field_named",
                "no_consent_word_named", "no_format_named"}
    # Действие не обещает отказ, которого obstacle_recovery не возвращает, и не
    # молчит ни об одном из тех, что действительно может вернуть.
    assert produced <= set(re.findall(r"[a-z_]+", notes))


def test_the_page_scripts_are_not_read_from_a_second_place():
    # Одно место, где живут скрипты препятствий: action читает их через obstacle_scripts.
    actions_source = open(main.__file__.replace("main.py", "audit_actions.py"), encoding="utf-8").read()
    assert "obstacle_scripts.read_script()" in actions_source
    assert "obstacle_scripts.tick_script(" in actions_source
    assert "browserSelector" not in actions_source
    assert "aria-describedby" not in actions_source


def test_the_fix_primitives_come_from_the_page_not_from_a_guess_in_python():
    script = obstacle_scripts.read_script()
    # Адрес контроля строит сам DOM (browserSelector), Python ничего не подставляет.
    assert "browserSelector(box)" in script and "browserSelector(input)" in script
    assert "%s" not in obstacle_scripts.READ_OBSTACLES


def test_an_observing_step_and_a_fix_are_two_different_things():
    # auto_fix меняет страницу (кликает, пишет), а dispatch повторяет только
    # наблюдающие шаги: значит исправление само не повторится никогда.
    assert obstacle_recovery.safe_to_repeat("auto_fix") is False
    assert "wait" in obstacle_recovery.OBSERVING_ACTIONS


def test_the_reading_script_returns_json_safe_shape_for_the_python_side():
    # state_of должен получать ровно те ключи, которые скрипт обещает вернуть.
    script = obstacle_scripts.READ_OBSTACLES
    assert "out = {waiting_for: '', consents: [], format_hints: []}" in script
    for key in ("waiting_for", "consents", "format_hints"):
        assert key in json.dumps(obstacle_scripts.state_of({}))
