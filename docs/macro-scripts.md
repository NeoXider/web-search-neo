# Macro scripts

[← back to README](../README.md)

A macro is a JSON file of `web_action` steps. Add a little logic to it and it becomes a
script: it decides, loops, waits for the page or a server, reuses other macros, and can run
in the background while you do something else. Same file, same `macro` action, same
dispatcher - every action is still validated against its published schema before it runs.

- [In one minute](#in-one-minute)
- [Step keys](#step-keys)
- [Control steps](#control-steps)
- [Expressions and templates](#expressions-and-templates)
- [Variables, results and the answer](#variables-results-and-the-answer)
- [Jobs: background, visible, stoppable](#jobs-background-visible-stoppable)
- [Asking the person: ask](#asking-the-person-ask)
- [Schedules](#schedules)
- [Inline scripts: run_steps](#inline-scripts-run_steps)
- [Checking before running](#checking-before-running)
- [Recipes](#recipes)
- [Limits and rules](#limits-and-rules)

## In one minute

```json
{
  "name": "publish-if-ready",
  "variables": {"url": null, "thumbnail": null, "tags": []},
  "steps": [
    {"action": "open", "url": "{{url}}", "session_id": "pub"},
    {"action": "upload", "selector": "#thumb", "file_paths": ["{{thumbnail}}"], "session_id": "pub",
     "when": "thumbnail"},
    {"action": "for_each", "items": "tags", "as": "tag", "steps": [
      {"action": "fill", "fields": {"#tag": "{{tag}}"}, "session_id": "pub"}
    ]},
    {"action": "run_script", "session_id": "pub", "save_as": "state",
     "script": "return {ready: !!document.querySelector('#ready'), errors: document.querySelectorAll('.error').length}"},
    {"action": "if", "test": "state.ready and state.errors == 0",
     "then": [{"action": "click", "selector": "#publish", "session_id": "pub", "expect": {"text": "Published"}}],
     "else": [{"action": "fail", "message": "not ready: {{state.errors}} error(s) on the form"}]},
    {"action": "return", "value": "{{state}}"}
  ]
}
```

`when` skips the upload when there is no thumbnail, `for_each` fills every tag, `save_as`
keeps what the page script returned, `if` decides, `expect` checks the page after the click,
`return` hands a value back. Run it like any macro:

```json
{"actions":[{"action":"macro","op":"run","name":"publish-if-ready","variables":{"url":"https://example.com/form","tags":["a","b"]}}]}
```

## Step keys

Any action step may carry these keys; they belong to the script, not to the action:

| Key | Meaning |
|---|---|
| `when` | expression; the step runs only if it is true (otherwise the trace says `skipped`) |
| `save_as` | keep the result as a variable: a `run_script` returned value (JSON text is decoded), otherwise the action's `data` |
| `assert` | expression over `result` (the whole step result) and `value` that must hold, or the step fails |
| `expect` | `test_run`'s page checks after the action: `selector` (+`selector_state`), `absent`, `text`, `no_text`, `url_contains`, `title_contains`, `script`, `timeout_seconds` |
| `retry`, `retry_delay` | repeat a failed step up to `retry` times (0-20), `retry_delay` seconds apart (default 1) |
| `on_error` | `"continue"` keeps going after a failure (then look at `last.ok`); default `"stop"` |
| `label` | a name shown in `results[]` and `trace` |

## Control steps

| Step | Keys | Does |
|---|---|---|
| `if` | `test`, `then`, `else` | runs one branch |
| `repeat` | `times` or `while` / `until`, `as`, `max_iterations`, `delay_seconds`, `timeout_seconds`, `steps` | loops; `while` is checked before a pass, `until` after |
| `for_each` | `items`, `as` (default `item`), `index_as`, `max_items`, `delay_seconds`, `steps` | one pass per item (a dict gives `{key, value}` items) |
| `wait_until` | `test`, `steps`, `timeout_seconds` (30), `interval_seconds` (1), `on_timeout: continue` | runs `steps` and checks `test` until it is true |
| `try` | `steps`, `catch`, `finally` | `catch` runs on a failure and sees `error.message`, `error.step`, `error.action` |
| `set` | `values` (or `name` + `value`) | sets variables (templates are rendered) |
| `sleep` | `seconds` | waits |
| `log` | `message` | adds a line to the answer's `log` (and a job's) |
| `fail` | `message` | stops the run as failed (a `try` can catch it) |
| `return` | `value` | stops the run successfully; the answer carries `returned` |
| `break`, `continue` | - | leave or skip a pass of the nearest loop |
| `call` | `name`, `variables`, `project_root` | runs another macro (5 deep at most, no cycles); `save_as` keeps its `return` value, or its outputs |
| `read` | `topic`, `params` | runs a `web_info` topic (`network`, `page_text`, `find`, `console`, `screen_state`, `page_elements`...); `output` defaults to `json` |
| `parallel` | `branches` (a list of step lists), `fail_fast` (default true) | runs the branches at once; each sees a copy of the variables and its `save_as`/`set` values come back after; give each branch its own `session_id` |
| `ask` | `message`, `options` (default yes/no), `timeout_seconds` (600), `default`, `save_as` | pauses until the person answers in the companion popup or a notification, or an agent calls `op=answer`; without `default` an unanswered ask fails |

`when` works on control steps too: a skipped `if` runs neither branch.

## Expressions and templates

`test`, `when`, `assert`, `while`, `until` and bare `items` / `times` are expressions. In any
other parameter write `{{expression}}`. A string that is one whole template keeps the
expression's type (`"times": "{{len(files)}}"` is a number, `"items": "{{files}}"` a list);
inside longer text the value is formatted (objects as JSON, null as empty).

The language is Python-like and safe - only this whitelist is accepted:

- `and`, `or`, `not` (also `&&`, `||`, `!`, `===`, `!==`), `== != < <= > >=`, `in`, `not in`,
  `+ - * / // %`, `a if cond else b`, lists `[...]`, dicts `{...}`;
- `a.b`, `a[0]`, `a["key"]`, `a[1:3]`, `list.length` - a missing key or index gives `null`;
- `true`, `false`, `null` (and Python's spellings);
- functions:

| Group | Functions |
|---|---|
| types | `len`, `str`, `int`, `float`, `bool`, `number` (first number in a text), `type`, `empty`, `default(value, fallback)` |
| numbers | `abs`, `min`, `max`, `round`, `sum` |
| text | `lower`, `upper`, `strip`, `contains`, `icontains`, `startswith`, `endswith`, `matches(text, regex)`, `search(text, regex)` (first group or match), `replace`, `split`, `join`, `json` (parse, null on failure), `to_json` |
| lists | `find(items, key=value)`, `filter(...)`, `count(...)`, `pluck(items, "a.b")`, `get(obj, "a.b", default)`, `keys`, `values`, `first`, `last`, `sorted(items, key=, reverse=)`, `unique`, `range` |
| time | `now()` (epoch seconds), `today()` (YYYY-MM-DD), `time_text(fmt)` |

`find`, `filter` and `count` take criteria as keywords: `find(net, method="POST",
url="~/api/upload")` - a value starting with `~` is a regular expression, anything else must
be equal; a key may be a dotted path. Given a read's whole answer they search its
`requests` / `items` / `results` / `rows` / `matches` list.

No imports, no attribute access to Python objects, no lambdas or comprehensions, no `**`:
an expression can read the run's data and nothing else. An unknown variable name is an
error naming the known ones (it is usually a typo); a missing key is just `null`.

Never put a template inside a `run_script` script - it would be pasted into the JavaScript as
text. Pass values in `args` and read `arguments[0]`, as before.

## Variables, results and the answer

- Inputs: what the file declares under `variables` (the value is the default) plus what the
  caller passes. In a script a declared `null` is a real "no value" that `when` can test; a
  name nobody declared, passed or produced is reported before the first step runs.
- Produced: `save_as`, `set`, loop variables (`as`, `index_as`), `error` inside `catch`.
- `last` always holds the previous step: `{ok, value, error, action, step}`.

A script run answers:

| Field | |
|---|---|
| `success`, `error`, `failed_step`, `failed_action` | `failed_step` is a path: `3`, `2.then.0`, `4.steps.1`, `0>child:2` inside a call |
| `returned` | the `return` value |
| `outputs` | every variable `save_as` / `set` gave a value |
| `log` | the `log` lines |
| `trace` | one short line per step - it survives `summary: "min"` |
| `results[]` | each executed action with `step`, `success`, `data` or `error`, `attempts` |
| `executed_count`, `failure_count`, `duration_ms` | |

## Jobs: background, visible, stoppable

Every script run is a job, whether it runs in the foreground or with `background: true`:

```json
{"actions":[{"action":"macro","op":"run","name":"gpt-image","background":true,"variables":{"prompt":"..."}}]}
```

A background run answers at once with a `job_id`; a foreground run answers when it ends, with
its `job_id` too. Then:

| Call | |
|---|---|
| `macro op=job job_id=<id>` | state (`starting`, `running`, `waiting`, `done`, `failed`, `cancelled`, `interrupted`), progress (step, action, executed), the last log lines, the result once finished |
| `macro op=job job_id=<id> wait_seconds=120` | waits up to that long for the end (max 3600) |
| `macro op=jobs` | every job: this server's and the ones other servers on this computer wrote |
| `macro op=cancel job_id=<id>` | stops it before the next step; a waiting step is interrupted |
| `macro op=answer job_id=<id> answer=<value>` | answers the job's open `ask` |

A job runs inside its server process, so other calls keep working while it waits - the
single-call CLI daemon included (it does not idle-exit while a job runs). Each job writes its
state to `~/.web-search-neo/macro-jobs/<job_id>.json` (`WEB_SEARCH_NEO_JOBS_DIR` moves it), so
another agent's server lists it and can stop it, and a restarted server still shows what ran
before; a job whose process is gone reads `interrupted` and is never resumed (its steps may
already have acted). The newest 50 finished jobs are kept.

**In the browser.** The companion popup's **Macros** card lists the running and recent jobs
with their step and progress, a **Stop** button, and the buttons of an open `ask`. A background
job's tabs carry a wrench in their favicon badge (a robot is an agent; both show when both act
on the tab) and the toolbar badge shows the wrench while a background job drives tabs. When a
background job ends, or asks something, Chrome shows a notification.

Give a job its own `session_id`: a job and the foreground driving one tab collide.

## Asking the person: ask

```json
{"action": "ask", "message": "Publish '{{title}}' now?", "options": ["publish", "keep draft"],
 "timeout_seconds": 1800, "default": "keep draft", "save_as": "choice"},
{"action": "if", "test": "choice == 'publish'", "then": [{"action": "click", "selector": "#publish", "session_id": "pub"}]}
```

The job waits in state `waiting` with the question in `op=job`'s answer. The person answers
from the popup or the notification's buttons; an agent answers with `op=answer`. With no
answer by `timeout_seconds`, `default` is used, or the step fails.

## Schedules

```json
{"actions":[{"action":"macro","op":"schedule","name":"price-watch","every_seconds":900,"variables":{"url":"..."}}]}
{"actions":[{"action":"macro","op":"schedule","steps":[...],"times":["09:00","18:30"],"max_runs":20}]}
```

A schedule runs a saved macro (`name`) or inline `steps` every `every_seconds` (10 at least)
or at `times` of day (local, `HH:MM`), until `max_runs`. Every run is an ordinary background
job. `op=schedules` lists them with the next run, the last job and its state;
`op=unschedule schedule_id=<id>` removes one. Schedules are kept in
`~/.web-search-neo/macro-schedules.json` (`WEB_SEARCH_NEO_SCHEDULES_FILE`), so they survive a
restart; one server process on the computer runs them (a heartbeat file), another takes over
when it stops, and a run still going when the next is due is skipped, never stacked.

## Inline scripts: run_steps

Logic is not only for saved files: `op=run_steps` takes the steps in the call, runs them as a
script and saves nothing.

```json
{"actions":[{"action":"macro","op":"run_steps","variables":{"q":"web automation"},"steps":[
  {"action":"search","query":"{{q}}","save_as":"found"},
  {"action":"if","test":"len(found.results) == 0","then":[{"action":"fail","message":"nothing found"}]},
  {"action":"return","value":"{{pluck(found.results, 'url')[:3]}}"}
]}]}
```

`op=validate` with `steps` (and no name) checks inline steps the same way it checks a file.

## Checking before running

`op=validate` reads the script and dispatches nothing. Besides the action checks it finds
expressions that do not parse, a `repeat` with no way to stop, `break`/`continue` outside a
loop, unknown keys on a control step, `save_as` names that would shadow `last`/`result`,
a `macro` action inside a macro (use `call`), and inputs nobody declared. Its answer lists
`inputs` (what the caller must give) and `produced` (what the run makes itself).

`op=preview` shows a script's steps with the inputs filled in (templates that need run-time
values stay as written) and an `outline`: one indented line per step.

## Recipes

**Wait for a server's answer, then act**

```json
{"action": "wait_until", "timeout_seconds": 120, "interval_seconds": 2,
 "steps": [{"action": "read", "topic": "network", "params": {"session_id": "s", "url_pattern": "api/save"}, "save_as": "net"}],
 "test": "find(net, method='POST', status=200) != null"}
```

**Check an API without a browser and branch on it**

```json
[{"action": "http_request", "url": "https://api.example.com/health", "save_as": "health"},
 {"action": "if", "test": "health.status != 200 or not contains(health.body, 'ok')",
  "then": [{"action": "fail", "message": "API is down: {{health.status}}"}]}]
```

**Optional field**: `"when": "promo_code"` on the fill step.

**Flaky element**: `"retry": 3, "retry_delay": 2` on the click, or `"on_error": "continue"`
and `{"action": "if", "test": "not last.ok", ...}` after it.

**Paginate until done**

```json
{"action": "repeat", "max_iterations": 50, "steps": [
  {"action": "run_script", "session_id": "s", "save_as": "page",
   "script": "return {rows: [...document.querySelectorAll('tr')].map(r => r.innerText), next: !!document.querySelector('a.next')}"},
  {"action": "set", "values": {"all": "{{default(all, []) + page.rows}}"}},
  {"action": "if", "test": "not page.next", "then": [{"action": "break"}]},
  {"action": "click", "selector": "a.next", "session_id": "s"}
]}
```

(`all` must be declared, e.g. `"variables": {"all": []}`.)

**Reuse a login**: put the login in its own macro and start others with
`{"action": "call", "name": "login", "variables": {"user": "{{user}}"}}`.

**Watcher**: a `repeat` with `delay_seconds` and a `wait_until` inside, started with
`background: true`, then read it with `op=job`; or `op=schedule` it every few minutes.

**Two sites at once**

```json
{"action": "parallel", "branches": [
  [{"action": "open", "url": "https://a.example/price", "session_id": "a"},
   {"action": "run_script", "session_id": "a", "script": "return document.querySelector('.price').innerText", "save_as": "price_a"}],
  [{"action": "open", "url": "https://b.example/price", "session_id": "b"},
   {"action": "run_script", "session_id": "b", "script": "return document.querySelector('.price').innerText", "save_as": "price_b"}]
]}
```

**Confirm before the irreversible step**: an `ask` before the publish or submit click (above).

## Limits and rules

- Loops stop at `max_iterations` / `max_items` (default 1000, at most 100000); a run stops
  at 20000 executed steps; `call` nests 5 deep; `sleep` and `wait_until` at most a day.
- `timeout_seconds` on `op=run` / `op=run_steps` bounds the whole run.
- A guarded macro (`guarded_stage` / `guarded_commit`) must stay a plain step list: one
  reviewed path to one terminal action.
- A macro without any script feature still replays as one batch, exactly as before.
