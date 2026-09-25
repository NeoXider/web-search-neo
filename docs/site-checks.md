# Checking your own site

[← back to README](../README.md)

Five actions help a developer look at a site the way a careful reviewer would before a
release: `security_report` (what is configured unsafely and how to fix it), `api_report`
(how the page talks to its own backend, and what each response says), `perf_report`
(how fast it loads), `har_export` (the network journal as a file) and `test_run` (a
regression scenario with a pass/fail verdict per step). They are for sites, APIs and
development servers you own or are allowed to test.

## What these checks do not do

The report is a configuration review, the kind Mozilla HTTP Observatory, securityheaders.com
or the Best Practices tab of Lighthouse produce. It is passive and bounded by construction:

- **Only the hosts you name.** The scope is the start URL's host (and port), plus the hosts
  you list; nothing the report itself sends goes outside it. A redirect, a link or a sitemap
  entry that leads outside is named in the answer, not followed. A host named without a
  port covers its default ports (80, 443) only, so a link to another port of the same
  machine is not followed either; a host named with a port covers that port only
  (`example.com:80` does not cover `https://example.com/`). The one exception is the
  ordinary browser load of `browser: true` (below): like any visit, the page's own
  subresources and scripts may reach other sites, and those requests are reported apart.
- **Only ordinary GETs**, the ones any visitor or search engine makes: the pages, `http://host/`
  (no path, no query - to see whether plain http redirects), `https://host/` for an http page
  on the default port (the HSTS read), `/.well-known/security.txt` (and the legacy
  `/security.txt`), `/robots.txt`, a crawl's `sitemap.xml`, and one ordinary verifying TLS
  handshake per https host. No cookies are sent (every request starts with an empty jar), and
  no credentials unless you put them into the URL yourself (`https://user:pass@staging...`).
- **No guessing.** Pages come from the URL, the `paths` you list, links in served pages and
  the sitemap - never from a wordlist. No port scanning, no origin enumeration for CORS (the
  page is read as served, without an `Origin` header), no protocol or cipher scanning, no
  payloads, no fuzzing, no attempts to get past a login, a CAPTCHA or bot detection.
- **Counted.** Every request goes through one budget (200 per call) and is listed, with
  credentials and sensitive query values redacted, in `requests_made`. What the budget
  refused is counted in `request_budget.refused`.
- With `browser: true` (the default, `scope: "page"` only) the page also loads once in a
  fresh isolated headless browser, exactly as any visit: the page, its subresources and
  whatever its scripts fetch. That traffic is summarised apart, in `browser_requests`. If the
  browser ends on a page outside the scope (a redirect or a script navigated it), that page
  is not analysed: the page checks read the served HTML of the checked URL, and
  `browser_requests.left_scope` names where the browser went.

Cookie and token values never appear in the report - only cookie names and flags.

## `security_report`

```json
{"actions":[{"action":"security_report","url":"https://staging.example.com/"}]}
```

| Parameter | Meaning |
| --- | --- |
| `url` | The start page, `http://` or `https://`. Optional only with `scope: "hosts"`. |
| `scope` | `page` (default): `url` (plus `paths`). `site`: a crawl from `url`. `hosts`: every origin in `hosts`, a section each. |
| `hosts` | Extra origins in scope, at most 10: `example.com`, `api.example.com`, `localhost:3000`, `http://127.0.0.1:8080`, `[::1]:8443`. Exact hosts only: wildcards (`*.example.com`), public suffixes (`com`, `co.uk`, `github.io`), bare LAN words, paths and credentials are refused before anything is sent. |
| `include_subdomains` | Also allow subdomains of every named domain (`cdn.example.com` under `example.com`). |
| `paths` | Your own routes to check too, on the start origin only (with `scope: "hosts"`, the first section): absolute paths (`/login`, `/api/health?probe=1`), at most 50, GET only, no `..`, no scheme, ASCII (percent-encode the rest). |
| `max_pages`, `max_depth`, `delay_ms`, `respect_robots` | The crawl of `scope: "site"`: at most `max_pages` pages (default 10, at most 50), `max_depth` links from the start page (default 2, at most 5), a pause of `delay_ms` before every page after the first (default 500; the origin's well-known files, sitemap and TLS handshake are read without it), `robots.txt` honoured unless `respect_robots: false`. |
| `browser` | `true` (default): render the start page in an isolated headless Chrome (`scope: "page"`). `false`: read the served HTML only. Crawled pages and `hosts` sections always read the served HTML. |
| `session_id`, `keep_open` | The isolated session the browser load uses (an already open one is refused) and whether it stays open afterwards (never after a failure). |
| `timeout_seconds` | Per request, 1-120. |
| `save_to`, `overwrite` | Also write the whole answer as JSON into the download folder. |

### Scope

- **`page`** checks one URL. With `paths`, each listed route is checked too and the answer
  becomes an aggregate (below); the origin's shared facts (redirect, TLS, well-known files)
  are read once.
- **`site`** reads `robots.txt` and the sitemap it names (else `/sitemap.xml`), then walks
  the links of the served pages breadth-first: the start page, your `paths`, sitemap entries,
  then links, one level of `max_depth` at a time. URLs are normalised (fragments dropped) and
  checked once. Every origin of the crawl gets its own facts - redirect probe, TLS handshake,
  well-known files and `robots.txt` - read once, the first time a page of it comes up.
  Skipped URLs are named in `not_crawled`: `robots` (Disallow), `out_of_scope` (other hosts
  or ports), `depth` (how many pages were not expanded), `budget` (when the request budget
  ran out); `queue_left` counts what `max_pages` left. A crawl follows GET links as a search engine would - without cookies,
  so it sees what an anonymous visitor sees.
- **`hosts`** checks each named origin's front page in its own section. A host named without
  a scheme is tried over https first and over http when https does not answer at all (a local
  dev server). With `url` as well, the first section is that URL.

An aggregate (`site`, `hosts`, or `page` with `paths`) carries `grade` and `score` of the
**weakest** graded page (`grade_rule` says so), `average_score`, `checked` and `graded`,
`sections` (each page's URL, grade, score, counts and top five fixes), a deduplicated
`priority` (one entry per problem with the `pages` it appears on and `page_count` -
headers are usually shared by every page), `reports` (the full per-page reports),
`requests_made` and `request_budget`.

### Local development servers

A local address - `localhost`, names under `.localhost`, `.local`, `.internal`, `.lan`,
`.home.arpa` and the other private-use suffixes, loopback and private-network IPs (10/8,
172.16/12, 192.168/16, fc00::/7) - is graded in development mode (`mode: "local_development"`).
A bare LAN word (`nas`) cannot be told apart from a top-level domain, so the scope refuses it:
use its IP address or its `.local` / `.lan` name. In development mode:

- https, HSTS, the http->https redirect, the certificate (a self-signed one included),
  cookie `Secure` flags, mixed content and forms/passwords over http are marked
  *not applicable in dev* (status `skip`, 0 points, the reason in `detail`);
- the headline `grade`/`score` covers everything else - CSP, framing, nosniff, Referrer-Policy,
  COOP/COEP/CORP, cookie `HttpOnly`/`SameSite`, SRI, the page;
- `as_served` keeps the plain numbers of the same findings, and `production_forecast` is the
  grade the same responses would get over https with a valid certificate, HSTS
  `max-age=31536000` and a same-host http->https redirect - a forecast only: check the https
  deployment itself before a release;
- a self-signed certificate is read once more without verification (as Observatory does), so
  an https dev server is graded in full.

Several dev servers (a front end and an API) are checked in one call with `scope: "hosts"`.

### What is checked

| Area | Checks |
| --- | --- |
| CSP | Header and `<meta>` policies parsed into directives; Observatory's verdict (below) plus advice on `object-src 'none'`, `base-uri`, `frame-ancestors` (header only), `form-action`, Report-Only. |
| HSTS | Present, `max-age` (15552000 s minimum, as Observatory), `includeSubDomains`, whether the header meets the preload requirements (the preload list itself is not queried). |
| Other headers | `X-Content-Type-Options: nosniff`, clickjacking (`frame-ancestors` or `X-Frame-Options`), `Referrer-Policy` (header and `<meta name="referrer">`, last recognised token wins), COOP, COEP, CORP, `Permissions-Policy` (wildcards on camera, microphone, geolocation, payment, ...), versions in `Server` / `X-Powered-By` / `X-AspNet-Version`, obsolete `X-XSS-Protection`. |
| Cookies | Observatory's result for the cookie jar of the page request, plus advice: credential-looking names without `HttpOnly`, script-set cookies without `Secure`, missing `SameSite`, broken `__Host-` / `__Secure-` prefixes, a `Domain` wider than the host or a public suffix. |
| Transport | https at all; `http://host/` redirecting to `https://` on the same host first; the certificate's issuer, subject, alt names, expiry and negotiated protocol, from one ordinary handshake. |
| CORS | `Access-Control-Allow-Origin` on the page response: `*`, `null`, `*` with `Allow-Credentials: true`. |
| Page | SRI of third-party scripts (Observatory, on the served HTML), mixed content (active and passive), forms posting to `http://`, password fields over http, in GET forms, without `autocomplete="current-password"`/`"new-password"`, `target="_blank"` links to other sites without `noopener`, inline scripts and `on*=` handlers that a strict CSP blocks (or that keep a loose CSP loose), third-party frames without `sandbox`, the other sites the page loads from. |
| Files | `security.txt` (RFC 9116: `Contact`, `Expires` in the future) and `robots.txt` (read only; a catch-all HTML page does not count as either). |

### The grade

`grade` and `score` reproduce **Mozilla HTTP Observatory v1.7.1** (source:
[mdn/mdn-http-observatory](https://github.com/mdn/mdn-http-observatory), tag `v1.7.1`,
`src/analyzer/tests/*.js`, `src/analyzer/cspParser.js`, `src/grader/charts.js`,
`src/scanner/index.js`). Observatory is MPL-2.0. The algorithms reproduce its behaviour -
the CSP policy merge that of `cspParser.js` v1.7.1 - and the Python code is written anew,
not translated line by line. `tests/test_observatory_parity.py` pins the behaviour case by
case ("this header, this cookie, this HTML -> this result and these points"); during
development the CSP parser and the CSP verdict were also compared with Observatory's own
code on 20 000 generated policies each, with no difference for printable-ASCII sources
(that comparison script is not part of the repository).

The arithmetic: start at 100, apply one result per test - the worst that applies - add the
bonuses only if the score with the penalties alone is 90 or more, floor at 0. Letters (the
score rounded down to a multiple of 5): A+ from 100, A 90, A- 85, B+ 80, B 70, B- 65, C+ 60,
C 50, C- 45, D+ 40, D 30, D- 25, F below 25. The maximum is 165; without a preload-list lookup
(below) this report can give at most 160. Only responses with status 2xx, 3xx, 401 or 403
are graded; any other status gets `grade: null`, `not_graded` and the findings.

| Test | Results and points |
| --- | --- |
| CSP | none, or Report-Only only -25; invalid -25 (an empty policy or trailing comma, a directive repeated in one policy, `'strict-dynamic'` without a nonce or hash); `'unsafe-inline'` or `data:` in script sources, or `*` / `http:` / `https:` / `ftp:` / `http(s)://*` / `http(s)://*.*` in script or object sources (unset ones fall back to `default-src`, then `*`) -20; `http:`/`ftp:` sources for active content on https -20; `'unsafe-eval'` in script or style sources -10; `http:`/`ftp:` images or media on https -10; unsafe style sources only 0; a repeated `report-uri`/`report-to` turns a passing result into 0; `default-src 'none'` +10; otherwise +5. |
| Cookies | session cookie (name contains `login` or `sess`) without `Secure` -40; session cookie without `HttpOnly` -30; anti-CSRF cookie (`csrf`) without `SameSite` -20; an invalid `SameSite` (empty, not Lax/Strict/None) or `SameSite=None` without `Secure` -20; session cookie without `Secure` but passing HSTS -10; any cookie without `Secure` -20 (-5 with passing HSTS); otherwise 0, and +5 only when every cookie has `SameSite`; no cookies 0. |
| Redirection | `http://host/` serves without redirecting -20; the chain never reaches https -20; a certificate error on the way -20; the first hop stays on http -10; the first hop leaves the host while switching to https -5; plain http not served 0. |
| HSTS | missing -20; invalid (no `max-age`, or a comma: the header sent twice) -20; no https -20; invalid certificate -20; `max-age` under 15552000 -10. |
| X-Frame-Options | `frame-ancestors` anywhere in the combined CSP (header or `<meta>`) +5; `DENY`/`SAMEORIGIN` +5; `ALLOW-FROM` 0; missing, empty or invalid -20. |
| X-Content-Type-Options | exactly `nosniff` 0; missing or anything else -5. |
| Referrer-Policy | the last recognised token of header plus `<meta>`: `no-referrer`, `same-origin`, `strict-origin`, `strict-origin-when-cross-origin` +5; `origin`, `origin-when-cross-origin`, `unsafe-url`, `no-referrer-when-downgrade` -5; nothing recognised -5; neither sent 0. |
| COOP | `same-origin`, `same-origin-allow-popups`, `noopener-allow-popups` +10; `unsafe-none` or missing 0; invalid or sent twice -5 (read as one RFC 8941 item: a token, parameters allowed). |
| COEP | `require-corp`, `credentialless` +10; `unsafe-none` or missing 0; invalid or sent twice -5. |
| Cross-Origin-Resource-Policy | `same-origin` / `same-site` +10; `cross-origin` or missing 0; anything else -5. |
| SRI | on the HTML the server sent: third-party scripts without SRI over http (or protocol-relative) -50, with SRI -20; without SRI over https -5; every third-party script with SRI over https +5; own scripts with SRI +5; only own scripts, no scripts, or not HTML 0. |
| CORS | Observatory's -50 needs a preflight with an `Origin` header, which the report never sends; not scored in `points`. |

Details that decide edge cases, as Observatory decides them:

- **Merged CSP.** Header and `<meta>` policies are split on commas and merged: for a
  directive a later policy names, only sources both allow survive (none left means
  `'none'`). Sources are lowercased; a `*-src` directive with no sources is `'none'`.
  `<meta>` policies are read only when the content type is exactly `text/html` or
  `application/xhtml+xml`; `frame-ancestors`, `report-uri` and `sandbox` do not count there.
- **Nonces and `'strict-dynamic'` edit the fallback itself.** When script-src and style-src
  fall back to `default-src`, a nonce drops `'unsafe-inline'` from `default-src`, and
  `'strict-dynamic'` drops scheme and wildcard sources, `'self'` and `'unsafe-inline'` from it -
  which style-src and the http checks then see too.
- **The cookie jar** is the one a browser-like jar keeps from the page request (its
  redirects included; Observatory reads `robots.txt` without the jar): cookies with a public-suffix or foreign
  `Domain` and cookies breaking the `__Host-`/`__Secure-` prefix rules are dropped, the last
  `Set-Cookie` of a name/domain/path wins, `heroku-session-affinity` is ignored. The invalid
  `SameSite` check also reads the final response's raw `Set-Cookie` lines. Cookies of
  `robots.txt`, the `http://host/` probe and the https HSTS read are not graded.
- **Empty headers.** An empty `X-Frame-Options`, COOP, COEP or CORP counts as not sent; an
  empty `Referrer-Policy`, `Strict-Transport-Security` or CSP header counts as sent and invalid.
- **SRI** matches `http://`/`https://` URLs case-sensitively, treats `//host/x.js` as foreign
  and insecure, a full URL on the named host's registrable domain as own, anything else as
  relative (secure only on an https page). A worse result is never replaced by a better one.
- **Certificates.** After a certificate error the origin is read without verification from
  then on - the retry and every later request to it (`requests_made` marks them) - and graded
  in full; the invalid certificate costs
  -20 in redirection and -20 in HSTS, and `tls-invalid` / `tls-untrusted-page` say what is wrong.

Where the report knowingly differs from Observatory:

- the HSTS preload bonus (+5) needs the browsers' preload list, which is not looked up, so it
  is never given (`hsts-preload-ready` says when the header meets the requirements);
- CORS is not scored (above);
- the redirect check requests `http://host/` without the page's path and query (they may
  carry a token); for a front page that is the same request;
- the report grades the URL it was given: for an `http://` URL of a site that also serves
  https, Observatory would grade the https response instead;
- redirects are followed at most 5 hops (Observatory: 10); a hop the scope or the budget
  does not allow, or one the HTTP client refuses (plain http to a public host outside the
  scope, a public-to-private hop), is not requested and the chain up to it is graded. A
  chain cut at an http:// hop is never called "never reaches https" (its end is unknown):
  it gets at worst Observatory's -10 for a first redirect that stays on http, with
  `evidence.cut` saying the chain was cut at the scope boundary; a plain-http hop to a host
  in the scope (`include_subdomains` included) is followed. `redirect-out-of-scope` /
  `redirect-over-budget` / `redirect-refused` says why a page's own redirect stopped;
- CSP sources are sorted the way an English-locale collator orders printable ASCII (as
  Observatory's `Intl.Collator` does): no difference for printable-ASCII sources, while
  sources with non-ASCII characters may be ordered, and so merged, differently;
- `<meta>`-set CORP is ignored, as in Observatory.

Everything else is this report's own and never moves the grade. Advice (Permissions-Policy,
version disclosure, security.txt, TLS expiry, `target=_blank`, autocomplete, inline code,
CSP advice) carries 0 points. The problems that deserve a number carry `extra_points` instead
and feed a second, stricter score under `extended`:

| Own check | `extra_points` |
| --- | --- |
| Active / passive mixed content | -50 / -10 |
| Form posting to http; password field on an http page | -20 each |
| Password form using GET | -10 |
| `ACAO: null`; `ACAO: *` with credentials on the page response | -25; -10 |
| Broken `__Host-` / `__Secure-` cookie prefix (the browser drops the cookie) | -10 |

### Reading the answer

| Field | Meaning |
| --- | --- |
| `grade`, `score`, `summary_line` | The Observatory verdict in one glance (the line also names the extended grade). |
| `mode` | `production` or `local_development`; the latter adds `as_served`, `production_forecast` and `dev_note`. |
| `priority` | What to fix, in order: failures before warnings, then severity, then points. Each entry has `fix`. |
| `findings` | Every check: `id`, `category`, `status` (fail / warn / pass / info / skip), `severity`, `title`, `detail`, `fix`, `evidence`, `points`, and `extra_points` for own penalties. |
| `score_explanation`, `extended` | Observatory's arithmetic; the stricter score with the own penalties. |
| `headers`, `cookies`, `csp`, `third_parties`, `redirect`, `tls`, `files` | The facts the findings rest on. `third_parties.resource_timing_note` appears when the page filled Chrome's 250-entry resource-timing buffer. |
| `requests_made`, `browser_requests` | What the report sent, and what the page load itself requested. Every URL in the answer - routes, errors, evidence - is redacted: no userinfo, sensitive query values masked. |
| `browser_error`, `browser_note` | The browser could not load the page, or the page's scripts broke the snapshot; page checks then read the served HTML. |
| `scope_mode`, `sections`, `reports`, `not_crawled`, `queue_left`, `request_budget` | Aggregates only (above). |

`summary: "min"` (on `web_action` or inside the action) keeps the grade, counts, summary
line and the first five `priority` entries, marks the answer `summary_mode: "min"` and names
everything it left out in `summary_omitted` and `summary_clipped`.

### Example: the weak fixture from the test suite (excerpt)

The page is served on `http://127.0.0.1` (development mode) with no CSP, a session cookie
without flags, `Access-Control-Allow-Origin: *` with credentials, `Referrer-Policy:
unsafe-url`, `X-Powered-By: PHP/8.1.2`, a third-party script over http without SRI and a
password form using GET (`browser: false`):

```json
{
  "grade": "F", "score": 0, "mode": "local_development",
  "summary_line": "F (0/100, local development; with this report's own checks F): 3 high, 3 medium, 10 low. Fix first: Third-party scripts without SRI load over http or protocol-relative; Session cookies readable by JavaScript; No Content-Security-Policy.",
  "production_forecast": {"score": 0, "grade": "F", "assumes": "the same responses served over https with a valid certificate, HSTS max-age=31536000 and an http->https redirect on the same host"},
  "priority": [
    {"rank": 1, "id": "sri-missing-insecure", "severity": "high", "points": -50,
     "fix": "Load them over https:// and pin each with integrity=\"sha384-...\" crossorigin=\"anonymous\"."},
    {"rank": 2, "id": "cookies-session-no-httponly", "severity": "high", "points": -30,
     "fix": "Add HttpOnly to session cookies."},
    {"rank": 3, "id": "csp-missing", "severity": "high", "points": -25, "fix": "Send Content-Security-Policy, for example: ..."}
  ],
  "score_explanation": {"penalties": [
    {"id": "xcto-missing", "points": -5}, {"id": "framing-missing", "points": -20},
    {"id": "referrer-unsafe", "points": -5}, {"id": "csp-missing", "points": -25},
    {"id": "cookies-session-no-httponly", "points": -30}, {"id": "sri-missing-insecure", "points": -50}]},
  "cookies": [
    {"name": "sessionid", "secure": false, "httponly": false, "samesite": null, "path": "/", "source": "set-cookie"},
    {"name": "prefs", "secure": false, "httponly": false, "samesite": "None", "domain": "127.0.0.1", "source": "set-cookie"}
  ],
  "requests_made": ["GET http://127.0.0.1:22377/", "GET http://127.0.0.1:22377/.well-known/security.txt",
                    "GET http://127.0.0.1:22377/security.txt", "GET http://127.0.0.1:22377/robots.txt"]
}
```

The strong fixture (`default-src 'none'` CSP with `frame-ancestors 'none'`, nosniff,
`Referrer-Policy: strict-origin-when-cross-origin`, COOP `same-origin`, CORP `same-origin`, a
`__Host-` cookie with every flag, security.txt) gets A+ (145) in development mode, C+ (60)
`as_served` on plain http, and two remaining fixes: its inline `<script>` and `onclick=` are
blocked by its own CSP.

### Sending a header with a non-ASCII value

HTTP header values are ASCII. `set_extra_headers` refuses anything else (Chrome would send
`?` for every such character) and `http_request` cannot encode characters outside Latin-1.
Encode the value the way the receiving application expects: percent-encoding for a value
the application decodes itself (`X-User-Name: %D0%90%D0%BD%D0%BD%D0%B0`), or the RFC 8187
extended notation for a header parameter that supports it
(`Content-Disposition: attachment; filename*=UTF-8''%E2%82%AC%20rates.pdf`).

## `api_report`

```json
{"actions":[{"action":"api_report","url":"https://staging.example.com/"}]}
```

With `url` alone the page loads once in a fresh isolated browser that is closed afterwards
(`keep_open` keeps it). With `session_id` alone the calls of the page already open there are
analysed - open it first and work with it (or run a `test_run` scenario against it) so the
calls land in the session's network journal; with both, that session navigates to `url`
first. `wait_seconds` (default 2) is the pause after the load before the journal is read.
The report sends no requests of its own: `requests_made` is always empty, an error
response's body is read from Chrome's own memory (never a second request), and repeating a
call on purpose stays `replay_request`'s explicit job.

| Parameter | Meaning |
| --- | --- |
| `url`, `session_id`, `keep_open` | The page to analyse: a cold load in a fresh isolated session (closed unless `keep_open`), or the calls of the session already open. |
| `hosts` | Your other API hosts, at most 10 (the same exact-host rules as `scope`): their responses count as own too. |
| `wait_seconds` | How long the page's calls may take to land in the journal (default 2, at most 30). |
| `timeout_seconds` | Per request, 1-120. |
| `save_to`, `har_to`, `overwrite` | Also write the report as JSON (`save_to`) and the same journal as HAR 1.2 (`har_to`) into the download folder. A HAR can carry what its URLs carried: treat it like a secret. |

`endpoints[]` is the map of the page's backend calls in frequency order: method, path template
(numeric, UUID and long-hex segments as `{id}`), count, origins and channel (`xhr`, `fetch`,
`websocket`, `sse`, `beacon`). Own-site API responses get the full checks; other sites'
origins are classified, never contacted:

| Area | Checks |
| --- | --- |
| CORS | `Access-Control-Allow-Origin` against credentials (`*` with credentials and `null` refused, specific origins listed), preflight methods, headers and `max-age`. |
| Caching | `Cache-Control`/`Pragma`: `public` on a JSON or cookie-setting response is a finding (it fails when the response also sets a cookie); `no-store`/`private` passes; a missing policy warns. |
| Content-Type | `Content-Type` plus `X-Content-Type-Options: nosniff` on JSON. |
| Error bodies | 4xx/5xx bodies from Chrome's memory (at most 8, 4000 characters each): stack traces, server file paths, framework versions - snippets and URLs masked. Bodies Chrome did not keep are named, not silently skipped. |
| Transport | `ws://` against `wss://` (fails on an https page, warns on http), `http://` calls from an https page, API calls to other sites' origins. |
| Tokens | Cookies as name, domain and `Secure`/`HttpOnly`/`SameSite` flags with the value's format only; `localStorage`/`sessionStorage` by name and format only; JWT-like values as facts (`alg`, `exp`/`iat`, signature present - an `alg: none`/unsigned token fails high). Token and cookie values are never printed. |
| CSRF | `SameSite` on session cookies (`SameSite=None` without `Secure` fails), a visible token in state-changing requests, writes leaving your site without one. |
| URLs | Secrets in query strings (tokens, e-mail addresses, session ids) - parameter names only, values masked. |

### Reading the answer

| Field | Meaning |
| --- | --- |
| `requests_observed`, `api_calls`, `dropped` | Journal rows read, calls they hold, rows that fell out of the history. |
| `endpoints`, `endpoints_omitted` | The call map (the first 60) and how many further endpoints were left out. |
| `auth` | Cookies, `local_storage`, `session_storage` - names, flags and formats, never values (`storage_note` when the storage script failed). |
| `counts`, `summary_line`, `priority`, `findings` | The verdict: counts by severity, the one-line summary, the fixes in order (each with `fix`), every check. |
| `scope`, `requests_made` | What counts as own, and the empty list proving nothing was sent. |
| `api-headers-unavailable` | A companion session records no response headers, so the finding says the CORS, caching and Content-Type checks could not run for those responses instead of quietly passing. |

`summary: "min"` (on `web_action` or inside the action) keeps `counts`, `priority` and
`summary_line`, marks the answer `summary_mode: "min"` and names everything it left out in
`summary_omitted` and `summary_clipped`.

## `perf_report`

```json
{"actions":[{"action":"perf_report","url":"https://staging.example.com/"}]}
```

With `url` alone the page loads cold in a fresh isolated browser that is closed afterwards
(`keep_open` keeps it). With `session_id` alone the page already open there is measured;
with both, that session navigates to `url` first (warm cache). `wait_seconds` (default 5)
bounds the wait for the load event. After a failure a session the report opened is closed
even with `keep_open`, because the answer then carries no `session_id` to close it later.

The numbers come from the page's own Performance API: navigation timing (`ttfb_ms`,
`dom_content_loaded_ms`, `load_ms`, document size, protocol, redirects), paint (`fcp_ms`),
buffered `largest-contentful-paint` (`lcp_ms`, `lcp_element`) and `layout-shift` observers
(`cls`, the largest session window, input-driven shifts excluded), and resource timing
(count, transfer by type, the largest files, third-party share, `render_blocking` from
`renderBlockingStatus`, or the head's synchronous scripts and stylesheets on an older
Chrome). `ratings` uses the Web Vitals thresholds; `recommendations` names the fix for each
problem (slow TTFB, slow LCP, layout shift, render-blocking files, uncompressed text, files
over 300 KB, heavy pages, too many requests). Chrome keeps 250 resource timings by default
and a page cannot raise that before it loads; when the page reaches it, `resources.buffer_note`
says that later resources are missing from the totals. The page's answer is type-checked
field by field first, so a page that replaced the Performance API yields empty metrics, not
an error.

These are lab numbers from the automation browser - no throttling, its network, its
cache. Compare runs of the same page before and after a change; do not read them as what
real visitors experience.

## `har_export` and third-party requests

```json
{"actions":[{"action":"open","url":"https://staging.example.com/","session_id":"s"},
            {"action":"har_export","session_id":"s","save_to":"staging.har"}]}
```

The session's network journal (the newest 500 finished requests plus those still in
flight) becomes a HAR 1.2 file under the download folder (`WEB_SEARCH_NEO_DOWNLOAD_DIR`),
which DevTools, Charles, Fiddler and most HAR viewers import. It carries method, URL,
query, status, resource type, total time, transfer size, remote address, the request body
the journal kept and the security-relevant response headers the journal keeps. Request
headers and response bodies are not recorded, and the file's `comment` says so. The journal
keeps at most 4000 characters of a request body: a longer one is exported with its real
length in `bodySize` and a `postData.comment` saying it was cut, and a body the browser did
not hand over is `bodySize: -1` with a comment, never an invented empty body. `_dropped`
counts requests that fell out of the history, `_done: false` marks in-flight ones.
`third_party_only` exports only other sites' requests, `inline=true` also returns the
document when it is under 100 000 characters, and an existing file needs `overwrite`.
`Set-Cookie` values are replaced by `REDACTED` (names and attributes kept). A HAR can still
contain tokens from URLs and post data: treat it like a secret.

The same filter works on the live journal:

```json
{"topic":"network","params":{"session_id":"s","third_party_only":true}}
```

It keeps requests to other registrable domains than the page's (`cdn.example.com` is the
same site as `www.example.com`; `data:` and `blob:` URLs are never third party) and names
`first_party_site` and `third_party_sites`.

## `test_run`: a regression test of a form

```json
{"actions":[{"action":"test_run","url":"http://127.0.0.1:8000/signup","session_id":"reg","steps":[
  {"step_name":"form is there","expect":{"selector":"#signup","title_contains":"Sign up"}},
  {"action":"fill","fields":{"#email":"qa@example.com","#password":"correct horse"}},
  {"step_name":"submit","action":"click","selector":"#send",
   "expect":{"text":"Check your inbox","url_contains":"/welcome","no_console_errors":true,"no_failed_requests":true}},
  {"step_name":"the form cannot be sent twice","action":"click","selector":"#send",
   "expect":{"action_fails":true}}
]}]}
```

A step is an ordinary `web_action` object with two optional keys of its own - `step_name`
and `expect` - or an expect-only step. Every other key belongs to the action: `cookies` and
`macro` take a `name` of their own, and `{"action": "cookies", "op": "clear", "domain":
"example.com", "name": "sid"}` clears exactly that cookie. `session_id` is filled in for
every action that takes one. With `url` the session is opened first (isolated when it is
new) and closed at the end unless `keep_open`.

| `expect` key | Passes when |
| --- | --- |
| `selector` (+ `selector_state`: present / visible / clickable) | the element reaches that state |
| `absent` | no element matches (waits until it is gone) |
| `text`, `no_text` | the page's text contains / does not contain it (a string or a list) |
| `url_contains`, `title_contains` | the URL / title contains it |
| `script` | the JS condition (run_script semantics) becomes truthy |
| `no_console_errors` | no console error or uncaught exception since the step began |
| `no_failed_requests` | no failed or 4xx/5xx request since the step began |
| `action_fails` | the step's action fails (a negative test) |
| `timeout_seconds` | a number: how long each check of this step may wait (default: the run's `timeout_seconds`, 5) |

Every check runs as the same `wait` a hand-written batch would use, so it waits up to its
timeout. The whole plan is validated before anything runs - every action's arguments against
its published schema, every `expect` key and value - so a typo fails at once with the step
index, never halfway through a flow that already submitted something. A `test_run` cannot
start another one: directly, or through a saved macro whose steps contain one (refused while
the plan is checked; a macro that does not exist yet is refused when it runs).

The answer puts the verdict first - `success`, `passed` / `failed` / `skipped` / `total`,
`summary_line`, `failed_steps` - then `steps[]` with every check's result and `duration_ms`.
`stop_on_failure` (default true) skips the rest after a failure; `screenshot_on_failure=true`
saves a PNG per failed step into the download folder; `include_data=true` adds each action's
short result.

## Recipe: check your site before a release

1. `security_report {url, scope: "site"}` on the staging host (or `paths` with the entry
   pages: home, sign-in, checkout). Fix `priority` top-down; rerun until only accepted
   warnings remain.
2. `api_report {url}` for the pages that call a backend; fix `priority` top-down.
3. `perf_report {url}` for the same pages; compare with the previous release's numbers.
4. `test_run` with the scenarios that must never break (sign-up, sign-in, the main form),
   `no_console_errors` and `no_failed_requests` on the steps that submit.
5. For a failure, `open` the page in its own session and read `console`, `network
   {only_errors: true}` and `har_export` for the bug report.

## Recipe: check a dev server

```json
{"actions":[{"action":"security_report","scope":"hosts","browser":false,
             "hosts":["http://localhost:3000","http://localhost:8000"]}]}
```

Both servers are graded in development mode: fix the headers, cookies and CSP the headline
grade shows, look at `production_forecast`, and run the report again on the https staging
deployment before the release - that is where HSTS, the redirect and the certificate are
judged.
