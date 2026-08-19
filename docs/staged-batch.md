# Safe staged browser batches

`scripts/staged_batch.py` runs one repeated browser workflow in up to four named
sessions. All active items finish a stage before any item starts the next one.
That barrier is useful for menus and animated UI: open every menu, let the page
settle, then choose the exact option in the next stage.

The runner is deliberately a guardrail, not an autonomous form submitter:

- one workflow has 1–4 items and 1–4 concurrent sessions;
- `open_many` and `close_all` are refused;
- strings support `{variable}` templates from global `vars`, item `vars`,
  `{item_id}`, and `{session_id}`;
- an unexpected visible enabled field pauses only that item;
- questionnaire markers plus live controls pause the item even when a field was
  allowlisted; a boilerplate heading alone does not;
- every terminal stage needs fresh pre- and post-action gates;
- a terminal stage runs only with `--approve-terminal STAGE`;
- the terminal attempt is written to a state journal before the browser call.
  A timeout, crash, failed check, or rerun therefore never retries it silently.

Validate a workflow without touching Chrome:

```powershell
python scripts/staged_batch.py workflow.json --check
```

Run non-terminal stages. The process exits with code `3` if any item pauses:

```powershell
python scripts/staged_batch.py workflow.json
```

After reviewing the reported fresh precheck, explicitly approve one named
terminal stage:

```powershell
python scripts/staged_batch.py workflow.json --approve-terminal finish
```

By default the at-most-once journal is `workflow.json.state.json`, with atomic
cross-process claims beside it in `workflow.json.state.json.claims/`. Pass
`--state path.json` to keep them elsewhere. Do not delete either to retry an
uncertain terminal call; inspect the actual page instead.

## Workflow format

```json
{
  "version": 1,
  "max_concurrency": 4,
  "settle_seconds": 0.7,
  "vars": {
    "choice": "Exact option"
  },
  "allowed_form_fields": [
    "input[type='search']"
  ],
  "items": [
    {
      "id": "first",
      "session_id": "slot-1",
      "vars": {
        "url": "https://example.com/a"
      }
    },
    {
      "id": "second",
      "session_id": "slot-2",
      "vars": {
        "url": "https://example.com/b"
      }
    }
  ],
  "stages": [
    {
      "name": "open",
      "actions": [
        {
          "action": "open",
          "url": "{url}",
          "profile_mode": "current"
        }
      ],
      "verify": {
        "topic": "page_text",
        "params": {"mode": "main", "max_chars": 3000},
        "path": "text",
        "contains_any": ["Expected heading", "Expected fallback"]
      }
    },
    {
      "name": "choose",
      "actions": [
        {
          "action": "click_text",
          "text": "{choice}",
          "exact": true,
          "role": "option",
          "terminal": false
        }
      ],
      "verify": {
        "topic": "page_outline",
        "params": {"limit": 120},
        "path": "outline",
        "contains_all": ["{choice}"]
      }
    },
    {
      "name": "finish",
      "terminal": true,
      "actions": [
        {
          "action": "click_text",
          "text": "Send",
          "exact": true,
          "role": "button"
        }
      ],
      "precheck": {
        "topic": "page_outline",
        "params": {"limit": 200},
        "path": "outline",
        "contains_all": ["button \"Send\""]
      },
      "verify": {
        "topic": "page_text",
        "params": {"mode": "main", "max_chars": 5000},
        "path": "text",
        "contains_all": ["Request sent"]
      }
    }
  ]
}
```

`allowed_form_fields` may be set globally or per stage and contains exact fresh
CSS selectors from `page_elements`. It acknowledges ordinary known controls;
it does not disable questionnaire detection.

Each gate calls a fresh `web_info` topic. Its optional `path` selects a nested
value such as `text` or `buttons.0.visible`. Assertions are `equals`, `truthy`,
`contains_all`, `contains_any`, and `not_contains`; text checks are
case-insensitive unless `case_sensitive` is true.

Actions use the normal `web_action` schema. The runner injects the item's
`session_id` and refuses an action that tries to target another item. `submit`
is always terminal. Common submit-like `click`/`click_text` labels and selectors
are also treated as terminal. Every ambiguous input action (`click`,
`click_text`, `input`, `pointer`, `press_keys`, or `touch`) must explicitly say
`"terminal": false`, or be the single action in a stage marked
`"terminal": true`. This makes a neutral selector such as `#primary-action`
impossible to submit by accident merely because its label hid the side effect.
