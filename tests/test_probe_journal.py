"""Журнал разведки: долговечная память о том, что уже пробовали и чем оно ответило.

Только файловая система и временные каталоги: ни сети, ни браузера. Корень
журнала подменяется аргументом root, а сквозной тест api.build — через
WEB_SEARCH_NEO_DOWNLOAD_DIR во временный каталог.
"""
from __future__ import annotations

import json

import pytest

from web_search_neo.audit import api, probe_journal
from web_search_neo.audit.probe_journal import (
    MAX_ENTRIES_PER_HOST, journal_file, query, record, seed_from_traffic,
)


def test_record_and_query_group_attempts_by_outcome(tmp_path):
    record("GET", "https://api.example.com/v1/users/123", 403, "access_scope",
           "rejected_by=edge", root=tmp_path)
    record("GET", "https://api.example.com/v1/users/456", 403, "access_scope",
           "rejected_by=edge", root=tmp_path)
    record("POST", "https://api.example.com/v1/chat", 402, "balance_or_plan",
           "rejected_by=application", root=tmp_path)
    out = query("api.example.com", root=tmp_path)
    assert out["attempts"] == 3
    assert sorted(out["by_outcome"]) == ["access_scope", "balance_or_plan"]
    # id-сегменты свёрнуты в шаблон, query отрезана, тела нет
    paths = {entry["path"] for entry in out["entries"]}
    assert paths == {"/v1/users/{id}", "/v1/chat"}
    assert "123" not in json.dumps(out)


def test_duplicates_merge_into_counts(tmp_path):
    for _ in range(3):
        record("GET", "https://api.example.com/v1/a", 404, "http_404", root=tmp_path)
    out = query("API.EXAMPLE.COM", root=tmp_path)  # хост строчными
    assert len(out["entries"]) == 1 and out["entries"][0]["count"] == 3
    assert out["attempts"] == 3


def test_entries_are_capped_and_evictions_counted(tmp_path):
    for i in range(MAX_ENTRIES_PER_HOST + 5):
        record("GET", f"https://api.example.com/v1/r{i}", 404, "http_404", root=tmp_path)
    out = query("api.example.com", root=tmp_path)
    assert len(out["entries"]) == MAX_ENTRIES_PER_HOST
    raw = json.loads(journal_file("api.example.com", tmp_path).read_text(encoding="utf-8"))
    assert raw["evicted"] == 5


def test_journal_file_layout_and_no_bodies(tmp_path):
    record("GET", "https://api.example.com/v1/a?token=secret", 403, "access_scope",
           "note", root=tmp_path)
    path = journal_file("api.example.com", tmp_path)
    assert path.parent.name == "probe_journal"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1 and raw["host"] == "api.example.com"
    assert "secret" not in path.read_text(encoding="utf-8")


def test_query_of_unknown_host_is_empty(tmp_path):
    out = query("never.example.com", root=tmp_path)
    assert out == {"host": "never.example.com", "attempts": 0, "entries": [],
                   "by_outcome": {}, "evicted": 0}


def test_seed_takes_only_errors_and_sends_no_requests(tmp_path):
    rows = [
        {"id": "a", "url": "https://api.example.com/v1/admin", "method": "GET",
         "status": 403, "type": "xhr", "headers": {"cf-ray": "abc"}},
        {"id": "b", "url": "https://api.example.com/v1/ok", "method": "GET",
         "status": 200, "type": "xhr", "headers": {}},
        {"id": "c", "url": "https://api.example.com/v1/missing", "method": "GET",
         "status": 404, "type": "xhr", "headers": {}},
    ]
    seeded = seed_from_traffic(rows, {"a": "", "c": '{"code":"NOPE"}'}, root=tmp_path)
    assert seeded == {"recorded": 2, "hosts": ["api.example.com"]}
    out = query("api.example.com", root=tmp_path)
    assert out["attempts"] == 2
    by_path = {entry["path"]: entry for entry in out["entries"]}
    assert by_path["/v1/admin"]["note"] == "rejected_by=edge"
    assert by_path["/v1/missing"]["note"] == "rejected_by=application code=NOPE"


def test_api_report_seeds_and_exposes_the_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    rows = [{"id": "a", "url": "https://api.example.com/v1/admin", "method": "GET",
             "status": 403, "type": "xhr", "headers": {"cf-ray": "abc"}}]
    report = api.build(rows, page_url="https://console.example.com/",
                       hosts=["api.example.com"], bodies={"a": ""})
    journal = report["probe_journal"]
    assert journal["seeded_this_run"] == 1
    assert journal["hosts"]["api.example.com"]["attempts"] == 1
    assert report["requests_made"] == []  # журнал не шлёт запросов
    # второй прогон видит первый: память долговечна
    again = api.build(rows, page_url="https://console.example.com/",
                      hosts=["api.example.com"], bodies={"a": ""})
    assert again["probe_journal"]["hosts"]["api.example.com"]["attempts"] == 2


def test_record_without_a_host_records_nothing(tmp_path):
    assert record("GET", "not a url", 403, "x", root=tmp_path)["recorded"] is False
    assert record("GET", "data:text/plain,hi", 403, "x", root=tmp_path)["recorded"] is False

def test_a_root_that_is_already_the_journal_dir_does_not_double_nest(tmp_path):
    from web_search_neo.audit.probe_journal import journal_dir, record, query

    outer = tmp_path
    inner = tmp_path / "probe_journal"
    assert journal_dir(inner) == journal_dir(outer)
    record("GET", "https://api.example.com/v1/nested", 404, "http_404", root=inner)
    out = query("api.example.com", root=outer)
    assert any(item["path"] == "/v1/nested" for item in out["entries"])
    assert not (inner / "probe_journal").exists()

def test_the_route_oracle_names_a_gate_that_hides_existence(tmp_path):
    from web_search_neo.audit.probe_journal import route_oracle

    entries = [{"path": "/web/v1/orders/refunds", "status": 401},
               {"path": "/web/v1/orders/promotions", "status": 401},
               {"path": "/web/v1/orders/cancel", "status": 403},
               {"path": "/web/v1/refunds", "status": 401},
               {"path": "/web/v1/promotions", "status": 404},
               {"path": "/web/v1/usage", "status": 200}]
    verdicts = {item["prefix"]: item["verdict"] for item in route_oracle(entries)}
    assert verdicts["/web/v1/orders"] == "auth_before_matching"
    assert verdicts["/web/v1"] == "existence_discriminates"
    assert any(item["children_total"] >= 3 and item["verdict"] == "auth_before_matching"
               for item in route_oracle(entries))

def test_the_route_oracle_caps_its_child_list(tmp_path):
    from web_search_neo.audit.probe_journal import ROUTE_ORACLE_CHILDREN, route_oracle

    entries = [{"path": f"/web/v1/orders/name{i}", "status": 401} for i in range(30)]
    rows = [item for item in route_oracle(entries) if item["prefix"] == "/web/v1/orders"]
    assert len(rows) == 1
    assert rows[0]["children_total"] == 30
    assert len(rows[0]["children"]) == ROUTE_ORACLE_CHILDREN
    assert rows[0]["verdict"] == "auth_before_matching"
