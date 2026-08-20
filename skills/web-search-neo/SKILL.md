---
name: web-search-neo
description: Use the Web Search Neo MCP server for free web search without API keys, resilient multi-engine fallback, HTTP page fetching, the user's current signed-in Chrome with AI-group tabs, isolated browser profiles, accessibility-level page inspection, form filling and upload, page console and network diagnostics, screenshots, and deterministic canvas/WebGL game testing. Trigger when a task needs current web results, visible work in existing Chrome tabs, authorized browser state, reading or driving a rendered page, diagnosing a broken page, or frame-exact keyboard, mouse, and touch input through the two-tool web_info/web_action contract.
---

# Web Search Neo

This file is optional. Start with the server's compact runtime playbook,
`web_info(topic="skill")`. Use `web_info()` with no arguments only when you need its full
action/topic index, recipes, limits, and pitfalls. Optional names, types, and defaults are
not in that index. Before sending an optional parameter, read
`web_info(topic="action_schema", params={"action": "<name>"})` — it also answers for an observation topic, so
`params={"action": "network"}` lists what `network` accepts. Ask before guessing: a topic
refuses any parameter it does not list.

Two tools only. `web_info` reads state; `web_action` performs 1-32 ordered operations.
One `session_id` controls one page. Reuse it to continue on that page; use a different id
when a second reference page must remain open.

For reliable small-model automation, repeat one rigid loop: inspect fresh DOM, act once,
then verify fresh DOM/text. Tool success proves an event was dispatched, not that the user
outcome happened. After navigation or rerender, discard old selectors, refs, and images.

## Observation topics

- `page_outline`: roles, accessible names, states, `ref:<epoch>:N` handles, and boxes. Start
  here.
- `page_text`: readable rendered text. `mode="full"` is the whole body; `mode="main"` drops
  navigation and chrome. On a page that is one big form, `main` would be empty, so the reader
  falls back to the full page and says so with `fallback_used` and `mode_used`. `excluded_chars`
  and `excluded` say what is missing from the answer and why.
- `element_text`: the whole content of one element, not a clipped slice of the page. Pass
  `params.selector` (CSS, a fresh `ref:`, or a `a >>> b` piercing path); `params.full_text=true`
  reads `textContent`, which no rendering filter may drop, so a scrolled-out code block or a
  collapsed panel gives up its tail. `params.mode="html"|"outer"|"both"` returns markup
  (innerHTML / outerHTML / both plus text).
- `find`: `params.query="submit application"` returns ranked refs instead of a whole page.
  `low_confidence: true` means nothing on the page answers the query - the matches are
  guesses, so read the page instead of clicking one. `ambiguous: true` means two matched
  equally and order alone chose; say which you mean. `role` filters.
- `page_elements`: flat selectors — CSS, a piercing path inside a shadow root or frame, or
  `""` when none is unique — plus `<select>` options, form metadata, and
  `visible`/`hidden_reason` per entry. It has no selector filter and takes no
  `frame_selector`: it always counts the whole existing DOM, including rendered controls
  below the viewport. Paginate each category with `limit` plus `offset` until
  `range.<category>.next_offset` is null. Scroll only to materialize lazy/infinite content,
  then reread from `offset=0`. Only `[contenteditable="true"]` is listed as a field, so a bare
  `<div contenteditable>` shows up in `page_outline` and here not at all.
- `console`: console output and uncaught errors with stack frames. Pages through history
  with `since_seq` and `limit`, and keeps its own place, so it never competes with
  `game_probe` for entries.
- `network`: requests with status, type, ms, and size; `only_errors=true` to triage. Use
  `output="json"` to get the `id` that `network_body` needs. Capture is armed when the
  session takes its tab, so the first navigation is in the buffer; a tab claimed with
  `attach_tab` is recorded only from the claim onwards. `dropped` counts what the 500-entry
  buffer evicted, so an empty list plus a high `dropped` is not a quiet page.
- `screenshot`, `game_probe`, `browser_status`, `browser_tabs`, `search_status`.
- `execute_js`: run a JavaScript snippet in a session's page and read its JSON-serialisable
  return value. The escape hatch for state the DOM reads do not expose — `localStorage`,
  virtualised rows, framework stores — and for mutations without an input-shaped
  equivalent. `args` arrive as `arguments[0..n]`; long strings are clipped at 200k chars and
  reported as `{clipped, length, head}`.
- Every `web_info` result (dict payloads) also carries the current local date/time and
  UTC-offset region under the top-level `now` key — there is no separate time topic.

The outline and `find` cross open shadow roots and same-origin iframes; a cross-origin frame
is a stub, so pass its selector as `frame_selector` to read it.

## Locators

Plain CSS works everywhere and stays the default. In companion `current` Chrome, it is the
only action locator: never send `ref:` or a `>>>` piercing path to `click`, `fill`, `wait`,
`upload`, `submit.form_selector`, or an input action. Outline refs and piercing paths are
still useful for observation there, but they are not action locators.

In `temporary`, `persistent`, and `attach` sessions, `fill`, `upload`, `click`, `wait`, and
`submit.form_selector` may also accept a fresh ref such as `ref:3f9a1c04b7e25d18:12` or a
piercing path such as `#host >>> .inner`. `submit_selector` always needs plain CSS.

A `frame_selector` always names exactly one frame: CSS matching two is refused with the
count, everywhere. The accepted *forms* differ. `fill`, `click`, `wait`, `submit`, `upload`,
`render` and the `page_outline`/`page_text`/`find` topics take all three. The input actions
— `press_keys`, `pointer`, `touch`, `game_probe`, an `input` batch's pointer entries and
every `pointer_lock` operation — take plain CSS only: they aim by coordinate and need the
frame's box in the top-level page, so a ref or a `>>>` path is refused before any event. Do
not copy the outline's `#host >>> #frame` path into `game_probe` or `input`; give those a
CSS selector that is unique by itself.

Pass a ref back exactly as it was returned: the first field is the document it came from, so
a handle from a page you have since navigated away from resolves to nothing and the action
tells you to read `page_outline` again, instead of silently acting on a different element.
Re-read after any navigation or re-render.

Repeated visible text is not identity. Compare the exact `href`, control `value`, or a
stable returned attribute from a fresh `page_elements` read. Never choose by array index,
`nth-child`, or an old long CSS path alone.

## Search and fetch

Send a `search` action. Keep `engine="duckduckgo"`, `fallback=true`, and
`challenge_mode="fallback"` unless the user asks otherwise. Use `challenge_mode="manual"`
only when a visible three-minute human handoff is useful, and never claim the server solves
CAPTCHA. Use `fetch_text`, `fetch_links`, or `fetch_many` when the URLs are already known.

Plain `http://` to public hosts is refused; use `https://`. Loopback and private addresses
stay reachable, so local services work unchanged.

## Browser profiles

- `current` (default) drives the user's signed-in Chrome through the companion extension.
  New tabs enter group `🟢 AI`; `attach_tab` claims an existing `tab_id` without moving it.
  `open` on a claimed session does not navigate the user's tab - it takes a new one in the
  group and reports the tab it gave back as `left_claimed_tab`. `close` removes a tab the
  agent opened and leaves a claimed tab open; `close_all` follows the same rule for every
  session at once.
- Another agent may be driving the same Chrome: `attach_tab` on a tab it already holds is
  refused with who holds it. Pick a different tab or open your own; do not retry.
- Tabs open in the background and nothing steals the user's focus, so they keep working
  while you do. Hidden-tab throttling is compensated, so frames, timers and input behave.
  A session whose Chrome restarted (or whose companion self-updated) is dropped with an
  error saying to open again: reopen it, do not retry the action.
- If the companion is disconnected, read `browser_status` and call `setup_current_chrome`.
  It opens no page and touches no browsing data; a connected companion older than the
  bundled build is reloaded automatically, reported as `self_update`. When it does return
  `manual_steps`, show them to the user word for word and wait: nothing can install the
  extension, or reload a build older than 1.3.1, on their behalf.
- `auto` falls back to a headless temporary browser; `temporary` and `persistent` also
  default headless. `headless=false` explicitly permits a visible MCP-owned window.
  `attach` uses a Chrome you started with a DevTools port and preserves its window mode.
- No normal action should steal focus. `show` is the sole foreground opt-in; call it only
  when the user explicitly asks to see the controlled tab. It never changes window state.

## Forms

`fill` reads every value back off the control, so `filled` means the field holds what you
asked for and `field_values` says what it actually holds; a rejected value is an error, not
a success, and `success` is false whenever the `errors` map is non-empty. `field_values`
answers for every selector you sent, failures included, so a control refused before anything
was typed — disabled, readonly, no such `<option>` — still shows what it holds; `null` means
nothing could be read back, i.e. the selector matched nothing or the control is gone. The
browser's own tidying is not a refusal: trimmed whitespace on `email`/`url`, `\r\n`, and a
handler's case folding all count as filled, while `maxlength` truncation and a rewritten
value still fail. Checkboxes take `1/yes/y/on/check/checked` or
`0/no/n/off/uncheck/unchecked`. Only a `<select multiple>` takes a list, reads back as one,
and has its whole selection replaced by a scalar. Date, time, range and colour controls are
set rather than typed, so an unparseable value is refused without touching the control and
the error names the format. `upload`, and `fill`'s `files` key, *replace* an input's
selection instead of adding to it. Rich-text editors (`contenteditable`, e.g. TipTap,
ProseMirror, Slate, Quill) are written as a real edit — the whole content is selected and
the text inserted through the browser's input channel — so the editor's own model updates
and its change handler fires; the read-back is the editor's `textContent`.
`fill`, `click`, `submit`, `upload` and `wait` accept `frame_selector`; `wait`'s
`timeout_seconds` defaults to 10 and is respected as passed — ask for as long as
the target needs.
`challenge_detected` means a challenge is *blocking* — a widget, or a positioned ancestor of
it, covering at least half the viewport over the centre, so a dismissible modal with a
captcha in it does not count. `captcha_widgets` lists captchas that are merely present,
which you can ignore, and `captcha_scan_incomplete: true` means the walk stopped early, so
an empty list is not proof of absence. All ride on page summaries — `open`, `fill`, `click`,
`submit`, `upload`, `wait_challenge` and the `page_elements` topic — and not on
`page_outline`, `page_text` or `find`, which build no summary.

`fill` blurs every control it writes, which is how the last field fires its `change` event.
Focus therefore ends on the body: a following `press_keys(["ENTER"])` needs `target_selector`
to reach a field, and `submit` needs no focus at all.

For a choice widget, do not trust its remembered/default value. Open it, reread the options,
match exact visible text/value, click the visible option row instead of its hidden
radio/input, then reread the collapsed control after the rerender. A heading such as
"answer questions" is boilerplate, not proof of a question: only live enabled form
controls are questions.

For any consequential submit use this low-freedom guard:

1. Keep a local state `submit_attempted=false`.
2. From a fresh DOM, confirm the exact target (`href` where available) and every critical
   live choice: resume, account, price, recipient, consent, or irreversible option.
3. If anything is ambiguous, stop. Otherwise set `submit_attempted=true` as the terminal
   submit is clicked exactly once.
4. After any result or timeout, never click the same submit again. Inspect fresh URL, text,
   elements, console, and network; the first click may already have succeeded.
5. Stop immediately on terminal success text. Only a clearly separate intermediate
   questionnaire may have its own later final submit, after its live controls are inspected.

Inspect every batch result: `success=false`, validation error, or `challenge_detected` means
the step is not complete. Use DOM/text to prove labels and selected values. If submission
silently fails, check `console` and `network`.

## Page scripts and trusted clicks

When a page ignores a synthetic `click` (its handler checks `event.isTrusted`, or it reads
pointer position), retry the same target with `click.trusted=true`: a real trusted mouse
sequence is dispatched at the element's centre after scrolling it into view, exactly as a
human pointer would land. The click hits whatever is at that point, so re-read the DOM to
confirm the result; an element with no visible box is refused with a clear error instead of
silently falling back.

For state only the page holds — virtualised rows, framework stores — and for mutations with
no input-shaped equivalent, use `run_script` (web_action) or the `execute_js` topic
(web_info). Both run in the session's top document; `args` arrive as `arguments[0..n]` and a
JSON-serialisable return value comes back as itself. Prefer `fill`/`click`/`pointer` for
anything a user gesture should do, and never use a script where a form control read is
enough.

`run_script` takes `user_gesture=true` when the page gates what you need behind a real
click — clipboard writes, fullscreen, audible autoplay. It runs the same snippet as though a
person had just clicked; use it only for those APIs, because it bypasses the WebDriver route
that reports errors most precisely.

Web Storage has its own action: `local_storage` reads the whole store as a map, or one
`key`, and writes or deletes one key, in `local` or `session` `kind`. Reach for it before
writing a script for the same thing.

`cookies` reads every cookie as a full object — `secure`, `httpOnly`, `sameSite` and
`expires` included, which is what makes a site's session handling auditable rather than just
readable — and also sets and clears them. Filter with `domain` (substring) and `name` (exact).

`inject_script` registers code that runs *before* every document's own scripts, which is the
only way to patch an API a page captures on load. It survives navigation for the life of the
session; `op=list` shows what is registered and `op=remove` forgets one.

## Repeating a task: macros

A task you will do more than once — an application form, a login, a search-and-filter — is
worth recording instead of re-deriving. `macro op=record name=hh-apply` starts capturing;
drive the task once with ordinary actions; `macro op=save` keeps what dispatched. Only
actions that *succeeded* are recorded, so a wrong selector you corrected does not enter the
script. Every action is recordable, and a replay dispatches through the same validated loop:
`click` keeps its target whichever form you used (selector, `text` plus `role`, or `x`/`y`
coordinates), and the scripting, cookie, storage, captcha, stealth, and request-replay actions
record and replay just like `open`, `fill`, and `submit` do.

Before replaying a consequential task, call `macro op=preview name=review-form` with the
same `variables` planned for `run`. It resolves every placeholder and returns the exact
steps with `executed=false`, without dispatching an action or changing browser state. This
is the review point for a final URL, form values, an upload path, or a submit step.

Replay with `macro op=run name=hh-apply`. The parts that change between runs are
`{{placeholders}}` in the saved steps: edit them in with `op=save` and explicit `steps`, or
write them from the start, then pass `variables` on each run. A placeholder that is a whole
value keeps its type, so a recorded number stays a number. Every placeholder is declared in
the saved file, so `op=show` tells you what a macro wants without reading its steps, and a
run missing one names all of them at once rather than failing on the first.

Macros are files and outlive the server; `op=list` summarises them and `op=delete` removes
one. A macro cannot run another macro — run them in order instead. A replay reports like any
batch, so check `failure_count` and each `results[i].success`: a saved click path is exactly
as fragile as the page it was recorded from, and a site redesign invalidates it silently.

The macro engine is universal and domain-neutral. Never add recruitment, commerce, finance,
or other domain policy to core. Put domain rules in project-owned saved macros/configuration;
only neutral broadly useful examples belong in the engine repository.

Pass an existing absolute `project_root` to any macro operation to use that project's
`.web-search-neo/macros/` set. Without it, the existing user store remains active. Always pass
the same project root to `guarded_stage` and `guarded_commit`, because its idempotency ledger
is project-local too.

For a consequential flow, keep one explicit terminal `submit` as the last step and use:

1. `op=guarded_stage` with `guard.target_url`, equal `canonical_url`, optional domain-defined
   `identity_key`, explicit `allowed_hosts`, optional `denied_hosts`, a unique idempotency
   token, and an existing absolute `resource_path` uploaded by that run.
2. Live semantic assertions such as
   `{"result_index":2,"path":"data.text","contains":"Request 42"}`. Stage executes
   everything except Submit and returns no checkpoint if an action or assertion fails.
3. Review the staged result. Only then call `op=guarded_commit` once with its checkpoint.

Core has no built-in host categories: allow/deny policy is supplied by the macro or caller.
The project ledger reserves canonical target/identity, resource path, and token, and consumes
the checkpoint before Submit dispatch, so an ambiguous timeout cannot cause an automatic
retry. Inspect the destination response separately; the guard proves one attempt, not remote
acceptance.

## Captchas

`captcha` handles the widget that stops everything else. `mode=detect` only reports.
`mode=wait` hands the visible browser to the user and returns the moment the challenge
clears — the default when no solving service is configured, and the honest answer, because a
person clicking the box always works. `mode=solve` sends the sitekey to a configured service
and writes the returned token into the page; it needs `WEB_SEARCH_NEO_CAPTCHA_KEY`
(`WEB_SEARCH_NEO_CAPTCHA_HOST` picks the provider, 2captcha by default) and costs money per
solve. `mode=auto` solves when a service is configured and the widget exposes a sitekey, and
waits otherwise.

A solved token is not a submitted form: some sites submit from the widget's own callback and
some wait for the button, so re-read the page and submit if it did not. A captcha with no
sitekey — an image or a behavioural check — cannot be sent to a service at all and has to be
cleared in the page.

## Requests, headers, and staying unremarkable

`set_extra_headers` adds headers to *every* request the session makes until cleared — an
`Authorization` token, a custom `User-Agent`, an A/B cookie — without touching each call.
The set is replaced whole each time, so `set_extra_headers` with no `headers` clears it;
there is no per-header removal.

`replay_request` re-sends a request from inside the page, so it carries the page's cookies
and origin. Pass a `request_id` from `network` (read it with `output=json` for ids) to repeat
a captured request, or spell out `url`/`method`/`headers`/`body`. The response comes back with
status, headers and a clipped body — enough to check whether a session token still works, an
endpoint is rate-limited, or what a form's POST returns, without driving the form again.
Captured POST bodies are not retained, so pass `body` explicitly to resend one.

`stealth op=on` hides the usual automation tells (`navigator.webdriver`, plugin/language
shape) before the page's own scripts read them, which lowers how often a site *shows* a
challenge. It is not a solve: a serious anti-bot service fingerprints far more, and the
override cannot undo the `--enable-automation` switch Chrome launched with. Pair it with a
real profile (`profile_mode=current`), human-paced input, and `captcha` for what gets
through. `op=off` forgets it for future documents.

## Visual coordinate clicks

Screenshot modes are `viewport` (default), `full_page`, and `region`. Omit viewport
`width`/`height` to keep the actual window; current Chrome refuses explicit resize.
`region` requires page-CSS `x/y/width/height`; `full_page` errors above 3840x10000 instead
of silently returning a partial capture.

Yes: take a fresh viewport `screenshot`, then send, for example,
`{"action":"pointer","pointer_action":"click","x":640,"y":360}`. Coordinates are
viewport CSS pixels (or frame-local CSS pixels with `frame_selector`), not arbitrary
full-page image coordinates.

Only a viewport screenshot maps directly. Compare its PNG width/height with the reported
`viewport_width`/`viewport_height`; if they differ, scale each image axis proportionally.
A full-page or region image can contain offscreen pixels, so first scroll the target into
view and take a new viewport screenshot. After scroll, zoom, resize, navigation, animation,
or rerender, discard the old image and recapture. Verify the click from fresh DOM/text.

In background `current` Chrome, screenshots can be slow or unavailable while Chrome is not
painting an obscured window. DOM inspection and pointer actions still work; do not treat a
screenshot timeout as page failure or call `show` unless foreground was explicitly requested.

## Scrolling

`scroll` takes positive `delta_y` down and negative up. Omit `x`/`y` for the viewport
centre, provide both to pick the container painted under that point, or pass `selector`
(CSS, a fresh `ref:`, or an `a >>> b` piercing path): the element is brought into view,
then the wheel lands on its centre, so the *container* holding the element scrolls — the
way to reach the tail of a tall chat answer or code block. `selector` and `frame_selector`
are mutually exclusive; pierce the frame instead. `before`/`after` are the selected
document's window metrics, so an inner container can move while they stay unchanged.

## Input and games

Read `game_probe` first and reuse its `frame_selector` for input and render actions.

Use one `input` action for everything that must land in the same frame. Key entries take
`tap`, `hold`, or `release`; pointer entries take `click`, `double_click`, `hover`, `move`,
`drag`, `press`, `release`, or `wheel` with `delta_x`/`delta_y`, up to 16 of each kind in one
call. Coordinates are viewport- or frame-local, and a point that maps outside the window is
refused rather than dropped; `coordinate_mode="delta"` moves relatively and `"relative"` is
the unclamped mode for pointer lock. A held modifier reaches later mouse and touch events.
Keys include
`F1`-`F12`, `NUMPAD0`-`NUMPAD9`, `META`, arrows, and any printable character; releasing `w`
lifts a key held as `W`.

```json
{
  "actions": [{
    "action": "input",
    "session_id": "game",
    "key_actions": [
      {"key": "W", "action": "hold"},
      {"key": "S", "action": "release"},
      {"key": "SPACE", "action": "tap"}
    ],
    "pointer_actions": [
      {"action": "hover", "x": 640, "y": 360},
      {"action": "wheel", "x": 640, "y": 360, "delta_y": -240},
      {"action": "move", "x": 15, "y": -5, "coordinate_mode": "delta"}
    ]
  }]
}
```

Other input actions: `press_keys` for keyboard-only work — 1-8 `keys` plus `key_action`
(`tap|hold|release`), with `repeat`, `hold_seconds`, `focus_mode`, and `hold_frames`, which
keeps a tap down across N released frames in step mode — so one tap call releases
`hold_frames` frames per `repeat`, not one; `touch` for tap, swipe, and
multi-finger press/move/release; `touch_emulation` so a game's mobile code path runs at all;
`pointer_lock` for first-person controls. The keyboard verb is `key_action`, the pointer
verb `pointer_action`, the touch verb `touch_action`, and pointer lock's is `operation`,
because the dispatcher itself owns `action`.

`input`, `press_keys`, `pointer`, `touch`, and `step` all accept `include_summary=false`,
which skips the page read and returns only the action result. Use it while driving a game
frame by frame, and read the page explicitly when you actually want to look at it. The four
input actions also take `wait_seconds`, which already defaults to `0`; `step` does not take
it at all and rejects the call if you send it.

Three refusals arrive before anything reaches the page, so treat them as a fix to make, not
a retry: tapping a key the session already holds (release it first — a tap's release would
lift a key the session still counts down), pressing a touch id that is already down (`move`
or `release` that finger instead), and a point that maps outside the window or onto whatever
covers the frame there, which is named for you. After a *failed* `input` batch `held_keys`
over-reports on purpose, because any event in it may have landed — call `release_inputs`
rather than reading the list.

Render modes:

- `normal`: native page timing.
- `throttled`: continuous slow motion at `target_fps`, such as 10.
- `step`: nothing advances until `step {frames}` or an `input` action releases a frame.

Both gated modes freeze page time: `performance.now()` and `Date.now()` advance one fixed
frame delta per released frame and page timers are queued against that clock, so the game
never measures your thinking time as its `deltaTime`. A tapped key is held for the whole
released frame, which is what engines that poll key state per frame need.

Always `release_inputs` after holding input and restore `render` to `normal` before handoff
or close. Take a screenshot or read `game_probe` between batches; in step mode a screenshot
taken before the first frame still shows the old frame, and `game_probe` reports
`animation.animation_suspended` with `animation.fps: null` instead of measuring a gate you
are driving by hand.

A probe's `console_messages` holds only the warnings and errors new since the previous
`game_probe` call — the result says so in `console_scope` — so polling it in a loop is cheap
and its output never grows with the run. Each entry is delivered once: read
`console_messages` on every probe you make, because a result you drop takes them with it.
Use the `console` topic when you need history, `log`/`info` levels, or stack frames.

## Batch results

`web_action` runs up to 32 ordered actions and stops at the first failure. Check top-level
`success`, `failure_count`, `stopped_early`, and every entry in `results`. Use
`continue_on_error=true` when later actions are independent or cleanup must still run — a
game batch ending in `release_inputs` and `render mode=normal` skips both if an earlier
action fails, leaving the page frozen with keys held.
