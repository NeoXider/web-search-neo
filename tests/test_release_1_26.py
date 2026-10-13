"""1.26: С‡С‚Рѕ СЃС‚СЂР°РЅРёС†Р° РЅР°Р·РІР°Р»Р° СЃР°РјР° - РїРѕРІС‚РѕСЂ РЅР°Р±Р»СЋРґР°СЋС‰РµРіРѕ С€Р°РіР° Рё РёСЃРїСЂР°РІР»РµРЅРёРµ РµС‘ РїСЂРѕСЃСЊР±.

Р”РІР° Р±Р»РѕРєР° РѕРґРЅРѕРіРѕ СЂРµР»РёР·Р°: api_report С‡РёС‚Р°РµС‚ РѕРїСѓР±Р»РёРєРѕРІР°РЅРЅС‹Р№ РєРѕРЅС‚СЂР°РєС‚ origin'Р° Рё
СЃСЂР°РІРЅРёРІР°РµС‚ РµРіРѕ СЃ С‚РµРј, С‡С‚Рѕ СЂРµР°Р»СЊРЅРѕ С…РѕРґРёС‚ (Block A), Р° obstacle_recovery / auto_fix
С‡РёРЅСЏС‚ С‚Рѕ, С‡С‚Рѕ С‡РёРЅРёС‚СЃСЏ Р±РµСЃРїР»Р°С‚РЅРѕ, Рё РїРѕРІС‚РѕСЂСЏСЋС‚ С‚РѕР»СЊРєРѕ РЅР°Р±Р»СЋРґРµРЅРёРµ (Block B).
Chrome Р·РґРµСЃСЊ РЅРµ РЅСѓР¶РµРЅ: РїСЂРёРјРёС‚РёРІС‹ РїСЂРёС…РѕРґСЏС‚ РІС‹Р·С‹РІР°РµРјС‹РјРё Р°СЂРіСѓРјРµРЅС‚Р°РјРё.
"""
from __future__ import annotations

import json
from pathlib import Path

import web_search_neo.main as main
from web_search_neo import dispatch, obstacle_recovery
from web_search_neo.audit import api, api_contract, api_errors, api_reads, scope
from web_search_neo.contract import budget, notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_the_release_exposes_both_new_capabilities_within_the_contract_budget():
    document = main._capabilities()
    assert len(json.dumps(document)) <= budget.CONTRACT_BUDGET  # РѕР±С‰РёР№ Р±СЋРґР¶РµС‚ РєРѕРЅС‚СЂР°РєС‚Р° РЅРµ СЃРґРІРёРЅСѓС‚
    assert "api_report" in document["actions"] and "auto_fix" in document["actions"]
    refused = obstacle_recovery.retry_plan("submit", "timed out waiting for the click", 1, 3)
    assert refused["retry"] is False and refused["why"] == "action_changes_page_state"


def test_api_report_reads_the_published_contract_and_names_the_difference():
    rows = [{"method": "GET", "url": "https://api.example.test/v1/items?page=2", "status": 402,
             "type": "fetch"},
            {"method": "POST", "url": "https://api.example.test/v1/orders", "status": 201, "type": "xhr"}]
    spec = {"openapi": "3.0.0", "info": {"title": "Example API"},
            "paths": {"/v1/items": {"get": {"parameters": [{"name": "limit", "in": "query"}]}},
                      "/v1/orders": {"post": {}}}}
    fetched = {"ok": True, "status": 200, "content_type": "application/json", "text": json.dumps(spec)}
    collected = api_contract.collect("https://console.example.test/", rows, {"example.test"},
                                     [{"href": "https://api.example.test/openapi.json", "rel": "link"}],
                                     lambda _url: fetched)
    assert collected["found"] is True
    # РџСѓС‚СЊ РѕРїСѓР±Р»РёРєРѕРІР°РЅ Рё РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ; РїР°СЂР°РјРµС‚СЂ РѕРїСѓР±Р»РёРєРѕРІР°РЅ, РЅРѕ РµРіРѕ РЅРёРєС‚Рѕ РЅРµ С€Р»С‘С‚;
    # С‚Рѕ, С‡С‚Рѕ API РїСЂРёРЅРёРјР°РµС‚, РЅРѕ РЅРµ РїСѓР±Р»РёРєСѓРµС‚, РЅР°Р·РІР°РЅРѕ РѕС‚РґРµР»СЊРЅРѕ.
    assert collected["declared_endpoints"] == 2 and collected["observed_endpoints"] == 2
    assert collected["matched"] == 2
    drift = collected["parameter_drift"]
    assert [item["path"] for item in drift] == ["/v1/items"]
    assert drift[0]["accepted_not_published"] == ["page"]
    assert drift[0]["published_not_used"] == ["limit"]


def test_the_report_attaches_the_drift_and_names_it_as_a_finding():
    rows = [{"method": "GET", "url": "https://api.example.test/v1/items?page=2", "status": 402, "type": "fetch"}]
    spec = {"openapi": "3.0.0", "paths": {"/v1/items": {"get": {"parameters": [{"name": "limit", "in": "query"}]}}}}
    fetched = {"ok": True, "status": 200, "content_type": "application/json", "text": json.dumps(spec)}
    budget = api_reads.spec_budget()
    report = api.build(rows, page_url="https://console.example.test/",
                       hosts=["api.example.test"], links=[{"href": "https://api.example.test/openapi.json"}],
                       fetch_spec=lambda _url: fetched, budget=budget)
    assert "contract_drift" in report
    assert "api-contract-drift" in {item["id"] for item in report["findings"]}
    # РљР°Р¶РґС‹Р№ СЃР°РјРѕР·РІР°РЅРЅС‹Р№ Р·Р°РїСЂРѕСЃ api_report РЅР°Р·РІР°РЅ Рё СѓР»РѕР¶РёР»СЃСЏ РІ СЃРІРѕР№ РїРѕС‚РѕР»РѕРє: РЅРё РѕРґРёРЅ
    # РЅРµ РїРѕС‚РµСЂСЏРЅ РјРѕР»С‡Р° (refused - СЌС‚Рѕ С‚Рѕ, С‡С‚Рѕ РїРѕС‚РѕР»РѕРє РѕС‚РєР»РѕРЅРёР», Рё РѕРЅ СѓС‡С‚С‘РЅ).
    assert all(label.startswith("GET ") for label in report["requests_made"])
    assert len(report["requests_made"]) <= api_contract.SPEC_REQUEST_LIMIT
    assert len(budget.made) == len(report["requests_made"])


def test_the_contract_is_read_only_from_addresses_the_page_declared():
    calls: list[str] = []

    def fetch(url: str) -> dict:
        calls.append(url)
        return {"ok": False, "status": 404, "content_type": "", "text": ""}

    budget = api_reads.spec_budget()
    out = api_contract.collect("https://console.example.test/", [], {"example.test"},
                               [{"href": "https://api.example.test/openapi.json", "rel": "link"}], fetch, budget)
    assert out["found"] is False
    # РќРёРєР°РєРѕРіРѕ РїРµСЂРµР±РѕСЂР° РїСѓС‚РµР№: С‚РѕР»СЊРєРѕ РѕР±СЉСЏРІР»РµРЅРЅРѕРµ СЃС‚СЂР°РЅРёС†РµР№ Рё СЃС‚Р°РЅРґР°СЂС‚РЅС‹Рµ Р°РґСЂРµСЃР° origin'Р°.
    assert all(url.startswith("https://console.example.test/") for url in calls), calls
    assert not any("/v1/" in url or "/api/" in url for url in calls)
    # РќРё РѕРґРЅРѕРіРѕ Р·Р°РїСЂРѕСЃР° СЃРІРµСЂС… РїРѕС‚РѕР»РєР° Р°СѓРґРёС‚Р°, Рё РѕС‚РєР°Р· СѓС‡С‚С‘РЅ, Р° РЅРµ РїРѕС‚РµСЂСЏРЅ.
    assert len(calls) <= api_contract.SPEC_REQUEST_LIMIT == budget.limit
    assert budget.refused >= 1
    assert "No published API contract was found" in out["note"]


def test_the_audit_request_budget_stops_the_contract_reads():
    def fetch(url: str) -> dict:
        return {"ok": True, "status": 200, "content_type": "application/json",
                "text": json.dumps({"openapi": "3.0.0", "paths": {"/a": {"get": {}}}})}

    budget = api_reads.spec_budget()
    out = api_contract.collect("https://console.example.test/", [], {"example.test"},
                               [{"href": f"https://api.example.test/spec{i}.json", "rel": "link"}
                                for i in range(20)], fetch, budget)
    # РќРё РѕРґРЅРѕРіРѕ Р·Р°РїСЂРѕСЃР° СЃРІРµСЂС… РїРѕС‚РѕР»РєР°: РѕРЅ СЃС‚РѕРёС‚ Рё РІ Р±СЋРґР¶РµС‚Рµ Р°СѓРґРёС‚Р°.
    assert out["requests_made"] == api_contract.SPEC_REQUEST_LIMIT == budget.limit
    assert len(budget.made) == budget.limit
    # РџРѕС‚РѕР»РѕРє СЃСЂР°Р±РѕС‚Р°Р» С‡РµСЃС‚РЅРѕ: С‡Р°СЃС‚СЊ РєР°РЅРґРёРґР°С‚РѕРІ РЅРµ Р±С‹Р»Р° Р·Р°РїСЂРѕС€РµРЅР°, Рё СЌС‚Рѕ РЅР°Р·РІР°РЅРѕ.
    assert budget.refused >= 1


def test_the_shared_audit_budget_is_the_ceiling_the_contract_read_respects():
    # РџРѕС‚РѕР»РѕРє С‡С‚РµРЅРёСЏ РєРѕРЅС‚СЂР°РєС‚Р° - С‡Р°СЃС‚СЊ РѕР±С‰РµРіРѕ Р±СЋРґР¶РµС‚Р° Р°СѓРґРёС‚Р°, Р° РЅРµ РѕС‚РґРµР»СЊРЅС‹Р№ СЃС‡С‘С‚С‡РёРє.
    shared = scope.Budget()
    assert api_reads.spec_budget().limit <= shared.limit


def test_an_error_says_what_it_means_without_repeating_its_body():
    meaning = api_errors.error_meaning(402, {"code": "insufficient_balance",
                                             "message": "add credit to keep calling"}, {})
    assert meaning["hint"] == "balance_or_plan"
    assert meaning["class"] == "payment"
    # Р¤РѕСЂРјР° РѕС‚РІРµС‚Р°, Р° РЅРµ Р·РЅР°С‡РµРЅРёСЏ: С‚Рѕ, С‡С‚Рѕ РЅР°РїРёСЃР°РЅРѕ РІ С‚РµР»Рµ, РІ СЃРІРѕРґРєСѓ С„РѕСЂРј РЅРµ РєРѕРїРёСЂСѓРµС‚СЃСЏ.
    shapes = api_errors.error_shapes(
        [{"method": "POST", "id": "r1", "url": "https://api.example.test/v1/chat", "status": 402}],
        {"r1": json.dumps({"code": "insufficient_balance", "message": "add credit to keep calling"})})
    assert shapes[0]["body_shapes"][0]["shape"] == "object(2)"
    assert "insufficient_balance" not in json.dumps(shapes[0]["body_shapes"])
    # РЎРјС‹СЃР» РѕС€РёР±РєРё РЅР°Р·РІР°РЅ РѕС‚РґРµР»СЊРЅРѕ РѕС‚ С‚РµР»Р°: hint РµСЃС‚СЊ, Р° СЃР°Рј С‚РµРєСЃС‚ СЃРѕРѕР±С‰РµРЅРёСЏ - РІ messages[].
    assert shapes[0]["meaning"]["hint"] == "balance_or_plan"


def test_the_dispatcher_repeats_only_an_observing_step_the_page_asked_to_wait_for():
    granted = obstacle_recovery.retry_plan("wait", "please wait", 1, 2)
    assert granted["retry"] is True and granted["why"] == "observing_step_blocked"
    for name in ("submit", "click", "fill", "auto_fix"):
        refused = obstacle_recovery.retry_plan(name, "please wait", 1, 3)
        assert refused["retry"] is False and refused["why"] == "action_changes_page_state", name


def test_the_retry_ceiling_is_an_environment_switch_and_a_clamped_number(monkeypatch):
    monkeypatch.setenv(dispatch.RETRY_LIMIT_ENV, "0")
    assert dispatch.retry_limit() == 1
    monkeypatch.setenv(dispatch.RETRY_LIMIT_ENV, "900")
    assert dispatch.retry_limit() == 1 + obstacle_recovery.RETRY_LIMIT_MAX
    monkeypatch.delenv(dispatch.RETRY_LIMIT_ENV)
    assert dispatch.retry_limit() == 1 + obstacle_recovery.RETRY_LIMIT_DEFAULT


def test_auto_fix_applies_only_what_the_page_named_and_refuses_the_rest():
    # auto_fix С‡РёС‚Р°РµС‚ СЃС‚СЂР°РЅРёС†Сѓ СЂРѕРІРЅРѕ РѕРґРёРЅ СЂР°Р·: РІСЃС‘ РЅР°Р·РІР°РЅРЅРѕРµ РІ СЌС‚РѕРј РѕРґРЅРѕРј СЃРѕСЃС‚РѕСЏРЅРёРё.
    state = {"waiting_for": "Processing, please wait",
             "consents": [{"label": "I agree to the terms", "selector": "#terms"}],
             "format_hints": [{"label": "Expected format: DD.MM.YYYY", "selector": "#date"}]}
    applied: list[tuple] = []
    slept: list[float] = []

    answer = obstacle_recovery.auto_fix(lambda needle: {"found": False}, lambda: state,
                                        lambda selector, value: (applied.append((selector, value)),
                                                                 {"success": True})[1],
                                        slept.append)
    # РЎС‚СЂР°РЅРёС†Р° РЅР°Р·РІР°Р»Р° Р¶РґСѓС‰РёР№ СЃС‚Р°С‚СѓСЃ, С‡РµРєР±РѕРєСЃ Рё С„РѕСЂРјР°С‚ - РїСЂРёРјРµРЅРµРЅС‹ СЂРѕРІРЅРѕ СЌС‚Рё С‚СЂРё.
    assert {item["fix"] for item in answer["applied"]} == {"wait_when_asked", "consent_checkbox", "field_format"}
    assert applied == [("#date", "DD.MM.YYYY"), ("#terms", True)]
    assert slept  # РїР°СѓР·Р° СЂРµР°Р»СЊРЅР°: РµС‘ РѕС‚СЃС‡РёС‚Р°Р» С‚РѕС‚ Р¶Рµ РїСЂРёРјРёС‚РёРІ, С‡С‚Рѕ Рё dispatch


def test_an_ambiguous_page_is_left_to_the_agent_and_says_which_choice_it_is():
    answer = obstacle_recovery.consent_selector([{"label": "I agree to the terms", "selector": "#terms"},
                                                 {"label": "I accept the cookies", "selector": "#cookies"}])
    assert answer["apply"] is False and answer["why"] == "several_consent_labels"
    assert answer["labels"] == ["i accept the cookies", "i agree to the terms"]


def test_the_page_scripts_live_in_one_module_the_action_reads():
    actions_source = (PROJECT_ROOT / "web_search_neo" / "audit_actions.py").read_text(encoding="utf-8")
    assert "obstacle_scripts.read_script()" in actions_source
    assert "api_reads.LINK_SCRIPT" in actions_source
    assert "browserSelector" not in actions_source  # РЅРё РѕРґРЅРѕР№ РєРѕРїРёРё СЃРєСЂРёРїС‚Р° РІРѕ РІС‚РѕСЂРѕРј РјРµСЃС‚Рµ
    assert "aria-describedby" not in actions_source


def test_the_contract_says_how_a_step_repeats_and_what_auto_fix_refuses():
    action_notes = notes._ACTION_NOTES["auto_fix"]
    assert {"what_it_reads", "what_it_applies", "refusals", "limits"} <= set(action_notes)
    for refusal in ("several_consent_labels", "several_formats_named", "no_field_named"):
        assert refusal in action_notes["refusals"], refusal
    pitfalls = main._capabilities()["pitfalls"]
    assert any("observation repeats" in line and "asks to wait" in line for line in pitfalls)
    assert any("api_report" in step for step in main._capabilities()["recipes"]["release_check"])


def test_the_docs_and_the_skill_carry_both_blocks():
    skill = (PROJECT_ROOT / "skills" / "web-search-neo" / "SKILL.md").read_text(encoding="utf-8")
    site_checks = (PROJECT_ROOT / "docs" / "site-checks.md").read_text(encoding="utf-8")
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    playbook = (PROJECT_ROOT / "web_search_neo" / "contract" / "playbook.py").read_text(encoding="utf-8")
    examples = (PROJECT_ROOT / "web_search_neo" / "contract" / "examples.py").read_text(encoding="utf-8")
    assert "auto_fix" in skill and "WEB_SEARCH_NEO_RETRY" in skill
    assert "## When the page tells you what it wants" in skill
    assert "contract_drift" in site_checks and "error_meanings" in site_checks
    assert "## `auto_fix`: when the page says what it wants" in site_checks
    assert "## 1.26.0" in changelog
    # Playbook (С‚Рѕ, С‡С‚Рѕ РјРѕРґРµР»СЊ С‡РёС‚Р°РµС‚ РѕРґРёРЅ СЂР°Р·) Р·РЅР°РµС‚ РїСЂРѕ РєРѕРЅС‚СЂР°РєС‚, Р° pitfall - РїСЂРѕ РїРѕРІС‚РѕСЂ.
    assert "api_report {session_id}" in playbook
    assert "Only an observation repeats on its own, while the page asks to wait." in examples
