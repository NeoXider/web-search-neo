"""Что api_report читает из ответов бэкенда о самом API и чем его трафик расходится с контрактом.

Ни одного запроса к API-маршруту: на вход строки журнала страницы, тела ошибок из
буфера Chrome и (для drift) GET документов, которые сайт сам опубликовал как контракт.
Формы вместо значений - правило всего этого инструмента.
"""
from __future__ import annotations

import json

import pytest

from web_search_neo.audit import api, api_contract, api_errors

PAGE = "https://console.example.com/auth"
API = "https://api.example.com"


def _row(path: str, status: int, *, method: str = "GET", row_id: str = "r", headers: dict | None = None,
         origin: str = API, type_name: str = "xhr") -> dict:
    return {"id": row_id, "url": f"{origin}{path}", "method": method, "status": status,
            "type": type_name, "headers": headers or {}}


# --- формы ошибок -------------------------------------------------------------

def test_error_shapes_group_by_method_path_and_status():
    rows = [_row("/web/v1/admin/users", 403, row_id="a"), _row("/web/v1/admin/orders", 403, row_id="b"),
            _row("/web/v1/user/61807/profile", 404, row_id="c")]
    bodies = {"a": '{"code":"FORBIDDEN","msg":"admin access required"}',
              "b": '{"code":"FORBIDDEN","msg":"admin access required"}'}
    shapes = api_errors.error_shapes(rows, bodies)
    assert [(item["method"], item["path"], item["status"]) for item in shapes] == [
        ("GET", "/web/v1/admin/orders", 403), ("GET", "/web/v1/admin/users", 403),
        ("GET", "/web/v1/user/{id}/profile", 404)]
    assert shapes[0]["codes"] == [{"code": "FORBIDDEN", "count": 1}]
    assert shapes[0]["messages"] == ["admin access required"]
    assert shapes[2]["body_shapes"] == [{"shape": "empty", "count": 1}]


def test_error_bodies_report_shapes_and_never_values():
    body = api_errors.parse_body('{"token":"secret-value","amount":5,"items":[1,2],"flag":true,"none":null}')
    assert body["kind"] == "json" and body["shape"] == "object(5)"
    assert body["fields"] == {"amount": "number", "flag": "bool", "items": "list(2)",
                              "none": "null", "token": "string(12)"}
    assert "secret-value" not in json.dumps(body)


def test_a_plain_text_error_is_measured_not_quoted():
    body = api_errors.parse_body("<html>500 Internal Server Error</html>")
    assert body["kind"] == "text" and body["length"] == len("<html>500 Internal Server Error</html>")
    assert "Internal Server Error" not in json.dumps(body)


@pytest.mark.parametrize(("status", "body", "hint"), [
    (402, '{"code":"insufficient_balance","msg":"add credit"}', "balance_or_plan"),
    (403, '{"code":"FORBIDDEN","msg":"admin access required"}', "access_scope"),
    (429, '{"code":"rate_limited","msg":"too many requests"}', "rate_limited"),
    (422, '{"msg":"amount must be at least 5"}', "validation"),
])
def test_what_an_error_publishes_about_the_api(status, body, hint):
    meaning = api_errors.error_meaning(status, api_errors.parse_body(body), {})
    assert meaning["hint"] == hint


def test_a_payment_wall_is_not_reported_as_a_rate_limit():
    body = api_errors.parse_body('{"code":"insufficient_balance","msg":"top up"}')
    meaning = api_errors.error_meaning(402, body, {"retry-after": "30"})
    assert meaning["hint"] == "balance_or_plan"


def test_a_nested_error_message_is_read_and_its_parameter_named():
    body = api_errors.parse_body('{"error":{"code":"invalid","message":"scope is required and must be one of own or site"}}')
    meaning = api_errors.error_meaning(422, body, {})
    assert meaning["hint"] == "validation"
    assert meaning["named_parameters"] == ["scope"]


def test_rate_limit_names_the_headers_that_carry_it():
    meaning = api_errors.error_meaning(200, {"kind": "empty"}, {"x-ratelimit-remaining": "9",
                                                               "content-type": "application/json"})
    assert meaning["hint"] == "rate_limited" and meaning["limit_headers"] == ["x-ratelimit-remaining"]


def test_an_error_that_says_nothing_about_the_api_gets_no_hint():
    meaning = api_errors.error_meaning(500, api_errors.parse_body("upstream failed"), {})
    assert "hint" not in meaning


# --- кто отказал: ребро/WAF или само приложение ------------------------------

def test_an_empty_body_with_edge_headers_is_an_edge_rejection():
    body = api_errors.parse_body("")
    meaning = api_errors.error_meaning(403, body, {"cf-ray": "abc", "server": "cloudflare"})
    assert meaning["rejected_by"] == "edge"
    assert "hint" not in meaning  # порядок hint'ов не тронут: пустота ничего не публикует


def test_a_json_error_envelope_is_the_application_even_behind_a_cdn():
    body = api_errors.parse_body('{"code":"FORBIDDEN","msg":"admin access required"}')
    meaning = api_errors.error_meaning(403, body, {"cf-ray": "abc", "server": "cloudflare"})
    assert meaning["rejected_by"] == "application"
    assert meaning["hint"] == "access_scope"


def test_a_server_header_alone_can_name_the_edge():
    body = api_errors.parse_body("<html><h1>403 Forbidden</h1></html>")
    meaning = api_errors.error_meaning(403, body, {"server": "cloudfront"})
    assert meaning["rejected_by"] == "edge"


def test_without_edge_signals_or_an_envelope_the_rejector_is_unknown():
    body = api_errors.parse_body("upstream failed")
    assert api_errors.error_meaning(500, body, {})["rejected_by"] == "unknown"
    assert api_errors.error_meaning(200, {"kind": "empty"},
                                    {"x-ratelimit-remaining": "9"})["rejected_by"] == "unknown"


def test_error_shapes_carry_the_rejector_per_group_and_per_hint():
    rows = [_row("/v1/admin", 403, row_id="a", headers={"cf-ray": "x"}),
            _row("/v1/admin", 403, row_id="b",
                 headers={"cf-ray": "y", "content-type": "application/json"})]
    bodies = {"a": "", "b": '{"code":"FORBIDDEN","msg":"admin access required"}'}
    shapes = api_errors.error_shapes(rows, bodies)
    assert len(shapes) == 1
    # группа смешана: большинство решает; hint-запись несёт своё большинство
    assert shapes[0]["rejected_by"] in {"edge", "application"}
    assert shapes[0]["meaning"]["hint"] == "access_scope"
    assert shapes[0]["meaning"]["rejected_by"] == "application"


def test_report_carries_errors_and_meanings_without_new_requests():
    rows = [_row("/web/v1/admin/users", 403, row_id="a")]
    report = api.build(rows, page_url=PAGE, bodies={"a": '{"code":"FORBIDDEN","msg":"admin access required"}'})
    assert report["errors"][0]["meaning"]["hint"] == "access_scope"
    assert report["error_meanings"][0]["scope_words"] == ["admin"]
    assert report["requests_made"] == []


def test_third_party_errors_stay_out_of_the_api_verdict():
    rows = [_row("/broken.js", 503, origin="https://cdn.other.net", type_name="script")]
    report = api.build(rows, page_url=PAGE, bodies={})
    assert report["errors"] == [] and report["error_meanings"] == []


# --- drift опубликованного контракта и фактического трафика -----------------

SPEC = json.dumps({
    "openapi": "3.0.0", "info": {"title": "Example API"}, "servers": [API],
    "paths": {"/web/v1/admin/users": {"get": {"parameters": [{"name": "page"}]}},
              "/web/v1/orders/checkout": {"post": {}},
              "/web/v1/user/{uid}/profile": {"get": {}}},
})


def test_openapi_json_is_read_as_paths_methods_and_parameter_names():
    parsed = api_contract.parse_spec(SPEC)
    assert parsed["ok"] and parsed["format"] == "openapi_json" and parsed["title"] == "Example API"
    assert [item["path"] for item in parsed["endpoints"]] == ["/web/v1/admin/users",
                                                              "/web/v1/orders/checkout",
                                                              "/web/v1/user/{uid}/profile"]
    assert parsed["endpoints"][0]["query_names"] == ["page"]


def test_a_yaml_spec_is_read_without_a_parser():
    yaml = ("openapi: 3.0.0\npaths:\n  /web/v1/admin/users:\n    get:\n      responses:\n"
            "        '200':\n          description: ok\n  /web/v1/orders/checkout:\n    post:\n"
            "      responses: {}\n")
    parsed = api_contract.parse_spec(yaml)
    assert parsed["ok"] and parsed["format"] == "openapi_yaml"
    assert parsed["endpoints"] == [{"path": "/web/v1/admin/users", "methods": ["GET"], "query_names": []},
                                   {"path": "/web/v1/orders/checkout", "methods": ["POST"], "query_names": []}]


def test_a_document_that_is_not_a_contract_says_why():
    assert api_contract.parse_spec('{"hello":"world"}') == {"ok": False, "why": "no_paths_key",
                                                            "keys": ["hello"]}
    assert api_contract.parse_spec("")["ok"] is False


def test_compare_names_undocumented_traffic_and_unwalked_doors():
    rows = [_row("/web/v1/admin/users", 403), _row("/web/v1/me/keys", 200, row_id="k"),
            {"id": "x", "url": "https://cdn.other.net/x", "method": "GET", "status": 200,
             "type": "xhr", "headers": {}}]
    drift = api_contract.compare([api_contract.parse_spec(SPEC)], rows, {"example.com"})
    assert [item["path"] for item in drift["unpublished_in_use"]] == ["/web/v1/me/keys"]
    assert sorted(item["path"] for item in drift["published_never_called"]) == [
        "/web/v1/orders/checkout", "/web/v1/user/{id}/profile"]
    assert drift["matched"] == 1


def test_a_path_parameter_named_differently_is_the_same_route():
    rows = [_row("/web/v1/user/61807/profile", 200)]
    drift = api_contract.compare([api_contract.parse_spec(SPEC)], rows, {"example.com"})
    assert drift["unpublished_in_use"] == [] and drift["matched"] == 1


def test_query_parameters_the_api_accepts_but_never_published():
    rows = [{"id": "q", "url": f"{API}/web/v1/admin/users?page=2&role=admin", "method": "GET",
             "status": 403, "type": "xhr", "headers": {}}]
    spec = json.loads(SPEC)
    del spec["paths"]["/web/v1/admin/users"]["get"]["parameters"]  # page нигде не объявлен
    drift = api_contract.compare([api_contract.parse_spec(json.dumps(spec))], rows, {"example.com"})
    assert drift["parameter_drift"] == [{"method": "GET", "path": "/web/v1/admin/users",
                                         "accepted_not_published": ["page", "role"], "published_not_used": []}]


def test_a_parameter_the_contract_publishes_and_nothing_sends_is_listed_back():
    rows = [{"id": "q", "url": f"{API}/web/v1/admin/users?role=admin", "method": "GET",
             "status": 403, "type": "xhr", "headers": {}}]
    drift = api_contract.compare([api_contract.parse_spec(SPEC)], rows, {"example.com"})
    assert drift["parameter_drift"] == [{"method": "GET", "path": "/web/v1/admin/users",
                                         "accepted_not_published": ["role"],
                                         "published_not_used": ["page"]}]


def _fetch(text: str = SPEC):
    def fetch(url: str) -> dict:
        if url.endswith("/openapi.json"):
            return {"ok": True, "status": 200, "content_type": "application/json", "text": text}
        return {"ok": False, "status": 404, "why": "http_404", "text": ""}
    return fetch


class _Budget:
    def __init__(self, limit=6):
        self.limit, self.made = limit, []

    def take(self, label: str) -> bool:
        if len(self.made) >= self.limit:
            return False
        self.made.append(label)
        return True


def test_the_contract_is_read_from_the_origin_and_its_gets_are_listed():
    budget = _Budget()
    out = api_contract.collect(PAGE, [_row("/web/v1/me/keys", 200)], {"example.com"}, [],
                               _fetch(), budget)
    assert out["found"] and out["spec_formats"] == ["openapi_json"]
    assert out["requests_made"] == 6 and len(budget.made) == 6
    assert all(label.startswith("GET https://console.example.com/") for label in budget.made)


def test_without_a_published_document_the_report_says_so_instead_of_guessing():
    out = api_contract.collect(PAGE, [], {"example.com"}, [], _fetch("{}"), _Budget(limit=20))
    assert out["found"] is False and out["spec_candidates_probed"] == len(out["probed"])
    assert "No published API contract" in out["note"]


def test_the_request_budget_stops_the_search_and_says_it_stopped():
    budget = _Budget(limit=2)
    out = api_contract.collect(PAGE, [], {"example.com"}, [], _fetch(), budget)
    assert any(item.get("why") == "request_budget_exhausted" for item in out["probed"])


def test_a_link_the_page_declares_is_a_shorter_way_to_the_contract():
    links = [{"href": "/docs/api.json", "rel": "openapi"}, {"href": "/style.css", "rel": "stylesheet"}]
    found = api_contract.discover_specs(links, PAGE)
    assert found == [{"url": "https://console.example.com/docs/api.json", "rel": "openapi",
                      "kind": "api_spec"}]


def test_api_report_attaches_the_drift_and_finds_it_as_a_finding():
    rows = [_row("/web/v1/me/keys", 200)]
    report = api.build(rows, page_url=PAGE, bodies={}, hosts=[API], fetch_spec=_fetch(), budget=_Budget())
    drift = report["contract_drift"]
    assert drift["found"] and drift["unpublished_in_use"][0]["path"] == "/web/v1/me/keys"
    titles = [item["title"] for item in report["findings"] if item["id"] == "api-contract-drift"]
    assert titles and "not in the published contract" in titles[0]
    assert report["requests_made"] and all(label.startswith("GET ") for label in report["requests_made"])


def test_the_drift_check_never_breaks_the_report():
    def explode(url: str) -> dict:
        raise RuntimeError("transport went away")
    report = api.build([_row("/web/v1/me/keys", 200)], page_url=PAGE, bodies={}, fetch_spec=explode)
    assert report["success"] and "did not finish" in report["contract_drift"]["note"]


@pytest.mark.parametrize("page", ["not a url", "ftp://example.com/x"])
def test_a_page_that_is_not_http_gets_no_candidates(page):
    assert api_contract.spec_urls(page) == []

# --- sitemap как вторый опубличашы источник ---------------------------------

def test_sitemap_paths_are_read_from_the_document():
    xml = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           "<url><loc>https://api.example.com/model-pricing/deepseek</loc></url>"
           "<url><loc>not-a-url</loc></url>"
           "<url><loc>https://other.example.net/x</loc></url></urlset>")
    assert api_contract.sitemap_paths(xml) == ["/model-pricing/deepseek"]
    assert api_contract.sitemap_paths("") == []


def test_the_sitemap_cannot_pass_its_cap_silently():
    xml = "".join(f"<url><loc>https://api.example.com/p/{i}</loc></url>" for i in range(120))
    got = api_contract.sitemap_paths(xml)
    assert len(got) == api_contract.SITEMAP_PATH_LIMIT


def test_no_contract_but_a_sitemap_still_publishes_paths():
    def fetch(url: str) -> dict:
        if url.endswith("/sitemap.xml"):
            return {"ok": True, "status": 200, "content_type": "application/xml",
                    "text": '<urlset><url><loc>https://console.example.com/docs/a</loc></url></urlset>'}
        return {"ok": False, "status": 404, "why": "http_404", "text": ""}
    budget = _Budget(limit=40)
    out = api_contract.collect(PAGE, [_row("/web/v1/me/keys", 200)], {"example.com"}, [], fetch, budget)
    assert out["found"] is False and "No published API contract" in out["note"]
    srcs = out["published_paths_from_sitemap"]
    assert srcs["paths"] == ["/docs/a"]
    assert [(item["method"], item["path"]) for item in srcs["observed_not_published"]] == [("GET", "/web/v1/me/keys")]

def test_a_sibling_subdomain_the_page_links_to_can_publish_the_paths():
    def fetch(url: str) -> dict:
        if url == "https://docs.example.com/sitemap.xml":
            return {"ok": True, "status": 200, "content_type": "application/xml",
                    "text": ("<urlset><url><loc>https://docs.example.com/web/v1/self</loc></url>"
                             "<url><loc>https://docs.example.com/guide</loc></url></urlset>")}
        return {"ok": False, "status": 404, "why": "http_404", "text": ""}

    budget = _Budget(limit=40)
    links = [{"href": "https://docs.example.com/en/docs/guides/billing"}]
    out = api_contract.collect(PAGE, [_row("/web/v1/self", 200)], {"example.com"}, links, fetch, budget)
    assert out["found"] is False
    srcs = out["published_paths_from_sitemap"]
    assert sorted(srcs["paths"]) == ["/guide", "/web/v1/self"]
    assert srcs["observed_not_published"] == []


def test_a_foreign_host_the_page_links_to_is_never_read():
    def fetch(url: str) -> dict:
        if url == "https://other.example.net/sitemap.xml":
            return {"ok": True, "status": 200, "content_type": "application/xml",
                    "text": ("<urlset><url><loc>https://other.example.net/x</loc></url></urlset>")}
        return {"ok": False, "status": 404, "why": "http_404", "text": ""}

    budget = _Budget(limit=40)
    links = [{"href": "https://other.example.net/x"}]
    out = api_contract.collect(PAGE, [_row("/web/v1/self", 200)], {"example.com"}, links, fetch, budget)
    assert "published_paths_from_sitemap" not in out
