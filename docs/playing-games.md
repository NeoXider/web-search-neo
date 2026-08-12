# Playing a browser game

[← back to README](../README.md)

A browser game does not wait for a model. Between two tool calls a real page runs
hundreds of frames, gravity keeps pulling, enemies keep moving, and `deltaTime`
becomes whatever the agent's thinking time happened to be. Web Search Neo solves
this by gating the page's animation loop: the game advances only when the agent
says so, and every released frame is the same length.

This document is the long version of the [game section in the
README](../README.md#canvas-and-webgl-games). Everything here is expressed as
`web_action` / `web_info` arguments, so it can be copied into any MCP client.

- [Why frame gating exists](#why-frame-gating-exists)
- [Render modes](#render-modes)
- [A full playthrough](#a-full-playthrough)
- [Why a tapped key stays down for a whole frame](#why-a-tapped-key-stays-down-for-a-whole-frame)
- [Auto-repeat for held keys](#auto-repeat-for-held-keys)
- [Games inside an iframe](#games-inside-an-iframe)
- [Pointer lock and first-person controls](#pointer-lock-and-first-person-controls)
- [Touch games](#touch-games)
- [Watching what happens](#watching-what-happens)
- [Cleanup is not optional](#cleanup-is-not-optional)
- [What is verified, and what is not](#what-is-verified-and-what-is-not)

## Why frame gating exists

| Without the gate | With `render` in `throttled` or `step` |
| --- | --- |
| `performance.now()` and `Date.now()` run on the wall clock, so a game reads the agent's thinking time as one frame. | Both are frozen and advance by exactly `frame_delta_ms` (16.667 ms by default) per released frame. |
| A batch of quick calls arrives with a frame delta near zero, so physics barely moves. | Every released frame is the same delta, whether the previous call took 30 ms or 30 seconds. |
| `setTimeout`, `setInterval`, and `requestIdleCallback` fire on real time, out of step with the frames. | They are queued against the same virtual clock and run immediately before that frame's animation callbacks. |
| The state you screenshot is rarely the state you acted on. | Nothing moves between calls; the frame you released is the frame you observe. |

Two honest limits:

- promises and `queueMicrotask` are not gated, and `new Date()` still reports
  wall-clock time. Only `Date.now()` and `performance.now()` are frozen;
- the gate replaces JavaScript `requestAnimationFrame`, which covers typical
  canvas/WebGL and Unity WebGL loops. It does not touch video decoding, CSS
  compositor animations, or the monitor refresh rate.

The gate is installed into every new document of the session, so it survives a
page reload or a game iframe that swaps its own document. A `step` that lands on
a fresh document re-applies step mode once and reports `gate_reinstalled: true`.

### The clock never runs backwards

Stepping makes page time run *ahead* of the wall clock: sixty frames released
back to back advance `performance.now()` by a second while barely a moment has
passed outside. Returning to `normal` therefore cannot simply hand the native
clock back — that would drop page time by however much the stepping had gained,
measured at −5.9 s on a real run, which a game reads as a negative frame delta
and a physics engine reads as a reason to throw everything off the screen.

So the gap is kept. `performance.now()`, `Date.now()` and the timestamps handed
to `requestAnimationFrame` are monotonic across every mode change in either
direction, and stay ahead of wall time by whatever stepping earned. The gap only
ever grows. A session that has stepped even once keeps the clock wrapper for the
rest of its life, because the wrapper is the only thing that can keep applying
the offset; a session that never stepped gets its untouched native clock back.
The practical consequence is short: do not compare a page-side timestamp with
one you took in the agent, and do not expect `Date.now()` in the page to agree
with the wall clock after a run.

## Render modes

| `mode` | Behavior | Use it for |
| --- | --- | --- |
| `normal` | Removes the gate, restores real clocks and the page's own `requestAnimationFrame` loop. | Finishing up, or watching the game run by itself. |
| `throttled` | Continuously releases animation callbacks at up to `target_fps` (default 10, clamped 1-60). Page time is still frozen between frames. | Slow motion while you watch, without hand-driving frames. |
| `step` | Holds every queued animation callback. Frames are released only by the `step` action (1-120 frames) or by an input action, which releases one — except a `press_keys` tap, which releases [`hold_frames` per `repeat`](#why-a-tapped-key-stays-down-for-a-whole-frame). | Deterministic play, frame-exact input, reproducible runs. |

`render` reports `frame_delta_ms`, `time_frozen`, `timers_gated`,
`pending_callbacks`, and `input_advances_frame`. `step` reports `frames`,
`callbacks`, `pending_timers`, `frame_count`, and `virtual_now`.

The MCP `render` action also accepts `frame_delta_ms` (0.1-1000 ms),
`freeze_time`, and `gate_timers` if the defaults do not suit a particular
engine. One knob stays Python-only: `browser_tools.set_render_control(...,
key_repeat=False)` turns off [key auto-repeat](#auto-repeat-for-held-keys); the
MCP action always leaves it on.

## A full playthrough

The bundled fixture `tests/fixtures/games/platformer.html` is a deterministic
canvas platformer: one animation frame is exactly one physics tick, and nothing
in it reads wall-clock time. Serve it locally, which also keeps the run off the
public internet:

```powershell
python -m http.server 8000 --directory tests/fixtures/games
```

Its rules, which the loop below plays against: the player moves 5 px per tick
while a horizontal key is held, a pit spans x 240-340, a standing jump cannot
clear it, and the finish is at x 520.

### 1. Open the game

```json
{
  "actions": [{
    "action": "open",
    "url": "http://127.0.0.1:8000/platformer.html",
    "session_id": "game",
    "profile_mode": "temporary",
    "headless": true
  }]
}
```

`profile_mode="temporary"` with `headless=true` gives a clean, reproducible
window. Drop both to play in the user's visible Chrome instead — that is the
default. Loopback addresses stay reachable over plain HTTP; a public host would
have to be `https://`.

### 2. Look at what you opened

```json
{"topic":"game_probe","params":{"session_id":"game","sample_seconds":0.5}}
```

The probe answers with the canvas list (`selector`, `context` of `2d`/`webgl`/
`webgl2`, `width`/`height`, and the on-screen `rect`), `iframes` with their
selectors, `document_has_focus`, `navigation_ms`, sampled `animation.fps`,
`console_messages` with any warnings and errors, and the currently `held_inputs`.
Take the canvas selector from here — `#game` for this fixture — and the iframe
selector if the game lives in one. This first probe reports everything logged so
far; every later one reports only what is new since it, which is the contract
described under [Reading the console between
probes](#reading-the-console-between-probes).

### 3. Engage the gate

```json
{"actions":[{"action":"render","mode":"step","session_id":"game"}]}
```

From here the page is frozen. A screenshot taken now still shows the frame that
was on screen before the gate closed.

### 4. Hold the direction key

```json
{
  "actions": [{
    "action": "input",
    "session_id": "game",
    "target_selector": "#game",
    "key_actions": [{"key": "ARROW_RIGHT", "action": "hold"}]
  }]
}
```

`target_selector` gives the canvas keyboard focus without clicking it: a
synthetic click would reach the game as a shot or a jump. (`press_keys` exposes
the same step as `focus_mode`, which defaults to `focus`; `click` and `none` are
the alternatives.) The key stays down until it is released explicitly, and this
call also releases one frame.

### 5. Advance frames and read the state

```json
{"actions":[{"action":"step","frames":3,"session_id":"game"}]}
```

```json
{"topic":"page_text","params":{"session_id":"game","max_chars":400}}
```

The fixture mirrors `frame`, `tick`, `x`, `y`, `onGround`, `won`, and `deaths`
into a DOM status line, so plain `page_text` is enough to follow it. A game
without such a line is read with `screenshot` or `game_probe` instead.

### 6. Jump at the right moment

```json
{
  "actions": [{
    "action": "input",
    "session_id": "game",
    "key_actions": [{"key": "SPACE", "action": "tap"}]
  }]
}
```

With `ARROW_RIGHT` still held, tapping `SPACE` while `onGround` is true and
`x` is 200-235 clears the pit. The tap is pressed together with the rest of the
batch, stays down for the whole released frame, and is lifted afterwards.

Repeat steps 5 and 6 until the status line says `won=true`. In step mode the
game observes no intermediate input state: everything in one `input` action
lands before the single frame it releases.

### 7. Clean up

```json
{
  "actions": [
    {"action": "release_inputs", "session_id": "game"},
    {"action": "render", "mode": "normal", "session_id": "game"},
    {"action": "close", "session_id": "game"}
  ]
}
```

### The loop in one call

Up to 32 ordered actions fit in a single `web_action`, which is useful when the
next few moves are already known:

```json
{
  "actions": [
    {"action": "render", "mode": "step", "session_id": "game"},
    {"action": "input", "session_id": "game", "target_selector": "#game",
     "key_actions": [{"key": "ARROW_RIGHT", "action": "hold"}]},
    {"action": "step", "frames": 30, "session_id": "game", "include_summary": false},
    {"action": "input", "session_id": "game",
     "key_actions": [{"key": "SPACE", "action": "tap"}]},
    {"action": "step", "frames": 20, "session_id": "game", "include_summary": false},
    {"action": "release_inputs", "session_id": "game"},
    {"action": "render", "mode": "normal", "session_id": "game"}
  ]
}
```

`include_summary=false` skips the post-action page read, which is the only cost
worth trimming on a hot loop: `step` drops from about 11 ms to 8 ms, `input`
from 34 ms to 29 ms.

## Why a tapped key stays down for a whole frame

A synthetic `keydown` immediately followed by `keyup` is invisible to most
engines. Phaser, Godot, Unity WebGL, and hand-written canvas loops all poll key
state once per frame: if the key went down and up between two frames, the poll
never sees it, and the jump simply does not happen.

So a tap is not a pulse:

- in an `input` action, every `tap` entry is pressed together with the rest of
  the batch, the frame is released, and only then is the key lifted;
- in `press_keys` with `key_action="tap"` under step mode, the key goes down,
  `hold_frames` frames are released (1 by default, up to 30), and the key goes
  up afterwards. `repeat` runs the whole sequence again, up to 50 times — so this
  is the one input action that does not release exactly one frame: it releases
  `hold_frames × repeat` of them, and `frames_advanced` in the result is the
  number to trust;
- outside step mode a tap holds for `hold_seconds` (0.05 s by default) instead,
  because there is no frame to hang it on.

```json
{
  "actions": [{
    "action": "press_keys",
    "session_id": "game",
    "keys": ["SPACE"],
    "key_action": "tap",
    "hold_frames": 3,
    "focus_mode": "none"
  }]
}
```

`hold_frames: 3` is the "charged jump" case: the key is held across three
released frames. `focus_mode="none"` skips the focus step entirely once the
canvas already has focus.

Held modifiers work the same way across a batch: a key held with `hold` is
carried into subsequent mouse and touch events, so `Shift`-click and
`Ctrl`-click behave the way a user's would. A key held as `W` is released by
`w` as well, and the release dispatches exactly the character that was pressed.

Tapping a key this session is already **holding** is refused, in `press_keys` and
in an `input` batch alike, before anything is switched, focused or sent. A tap is
a press followed by a release, and the release would lift a key the session still
counts as down — the page would see it up while every later batch kept carrying
it. Release it first, or drop the tap. Two taps of the same key *inside one
batch* are not this case and simply collapse; only a hold from an earlier call
conflicts. Pressing a touch id that is already down is refused for the same
reason: Chrome ignores the second press, so `move` that finger or `release` it
first.

## Auto-repeat for held keys

A real keyboard does not send one `keydown` and go quiet — it repeats while the
key stays down. Many games rely on that without knowing it: they latch movement
on `keydown`, and they clear that latch themselves on death, respawn, a menu, or
a level restart. With a single synthetic `keydown` the latch is never re-armed,
so the character stays frozen even though the agent is still "holding" the key.

Before a call releases frames, the session therefore re-sends a `keydown` with
the repeat flag for each key it is holding. This happens once per call, not once
per released frame: a `step` of 30 frames delivers exactly one repeat, not
thirty, so an engine that counts `keydown` events sees one per input or `step`
action rather than one per tick. Two details matter:

- a key that was pressed *by this very call* is not repeated by it, the same way
  a real keyboard waits before it repeats;
- re-holding a key that is already down does not silence its repeat.

This is on by default and is not exposed through MCP. The Python API can turn it
off with `browser_tools.set_render_control("step", session_id, key_repeat=False)`
for an engine that treats repeats as new presses.

The bundled platformer clears its movement latch on respawn, which is exactly
the case that would otherwise dead-lock a run after the first death.

## Games inside an iframe

Game portals — Yandex Games among them — host the game in an iframe. Pass its
CSS selector as `frame_selector` to `render`, `input`, `press_keys`, `pointer`,
`touch`, and `pointer_lock`, and to the `game_probe`, `page_outline`,
`page_text`, and `find` topics. `step` has no `frame_selector` of its own: it
reuses the one the session's `render` call stored.

Probe the host first, then the frame:

```json
{"topic":"game_probe","params":{"session_id":"ufo"}}
```

```json
{"topic":"game_probe","params":{"session_id":"ufo","frame_selector":"#game-frame"}}
```

The first call describes the host document — for a pure portal page
`canvas_count: 0` — and lists the frame in `iframes` with the selector to use.
The second call reports the canvas inside it. Then gate the frame, not the host:

```json
{
  "actions": [
    {"action": "render", "mode": "step", "session_id": "ufo", "frame_selector": "#game-frame"},
    {"action": "input", "session_id": "ufo", "frame_selector": "#game-frame",
     "key_actions": [{"key": "SPACE", "action": "tap"}],
     "pointer_actions": [{"action": "hover", "x": 320, "y": 180}]}
  ]
}
```

Coordinates stay frame-local: `x` and `y` are measured from the top-left corner
of the frame's *content* box. Chrome's input events are addressed in top-level
page pixels, so the server maps every point before dispatching it. That map is a
full affine transform, composed from the frame's own CSS `transform` and every
ancestor's, with its translation read back off the painted bounding rect so scroll
and layout are already in it; the frame's left/top border and padding are the
origin it starts from. Adding that origin alone, which is all it used to do, put
every click in a scaled frame at roughly twice its offset — and a 4 px border on
its own was enough to miss by 4 px. A frame under a 3D transform is mapped by its
best flat approximation, and the server logs that it did.

A point inside the frame that maps to somewhere outside the window is **refused**,
with both coordinates and the advice to scroll the frame into view, rather than
dispatched into nothing. The one exception is `coordinate_mode="relative"` under
pointer lock, which is unbounded on purpose.

A point that lands *on* the page but not on the frame is refused too, and the
error names what is in the way — `div#banner`, or `nothing this document paints`.
A frame answers every question about its own viewport as if it were whole, so a
frame clipped by an `overflow: hidden` ancestor, or with a fixed header painted
over part of it, reports a perfectly good coordinate that Chrome then delivers to
the header. The same `elementFromPoint` question the browser asks before
delivering is asked first, which is why one check covers the clip and the overlay
alike. Mid-gesture interpolated points are exempt — a swipe may legitimately pass
under something — and if the document moves on between the measurement and the
test, it fails open rather than inventing a blocker.

The map also composes the individual `rotate` and `scale` CSS properties and CSS
`zoom`. Chrome reports `transform: none` for a frame styled with `rotate: 20deg`,
and `zoom` is not a transform at all, so a frame styled that way used to be aimed
at as though it were unstyled: the click landed elsewhere and the call reported
success. The `translate` property is deliberately not read — it is a pure
translation, and every translation in the chain is already recovered from the
painted bounding rect.

One thing this map does not accept is a piercing path. `frame_selector` on
`render`, `wait`, `click`, `fill` and the reading topics takes CSS, a `ref:`
handle or `#host >>> #frame`. On the input actions — `input`'s pointer entries,
`press_keys`, `pointer`, `touch`, `game_probe` and `pointer_lock` — it is plain
CSS and nothing else, refused before the first event rather than part way through
a batch, because the map needs the frame's box in the top document and neither
other form gives one. An ambiguous CSS selector is refused there too, with the
count, exactly as everywhere else. So a nested game frame the outline can only
name as `#host >>> #frame` needs a CSS selector of its own before it can be
played — and `game_probe` is on this side of the line as well. It dispatches
nothing, but the canvas rectangles it reports are aimed at with the same string,
so probing one of several matching frames and playing another was one defect, not
two. `pointer_lock` refuses the other forms on `release` and `status` as well,
even though only `acquire` sends a click; one call cannot read one string two
ways.

The host document keeps animating while only the frame is gated. `game_probe`
knows this: while a gate is engaged anywhere in the session its `animation`
object reads `fps: null`, `animation_suspended: true`, and a `reason` naming the
gated frame, instead of reporting the host's healthy frame rate for a frozen
game.

## Pointer lock and first-person controls

```json
{
  "actions": [{
    "action": "pointer_lock",
    "operation": "acquire",
    "session_id": "fps",
    "selector": "#game"
  }]
}
```

`requestPointerLock` needs a user gesture, so `acquire` first dispatches a real
click at the centre of the target and requests the lock from that gesture — the
game will see that click. Without `selector` the first `<canvas>` is used, or
`<body>` if there is none.

While locked, the cursor cannot move, so absolute coordinates mean nothing; only
`movementX`/`movementY` reach the page. Use `coordinate_mode="relative"`, which
skips the viewport bounds check and moves by an unclamped delta. Chrome derives
`movementX`/`movementY` from consecutive dispatched positions, so a relative run
continues from wherever the pointer last was — the acquiring click counts as that
position. It used to warp to the viewport centre first, and to recentre
periodically, and the page read each warp as the movement: a first `(3, 2)`
arrived as `(470, 272)`, and a recentre as `-99281`. Every move now reports
exactly the delta it was given:

```json
{
  "actions": [{
    "action": "input",
    "session_id": "fps",
    "pointer_actions": [{"action": "move", "x": 400, "y": -30, "coordinate_mode": "relative"}]
  }]
}
```

`operation="status"` reports the current state, and `operation="release"` exits.
`coordinate_mode="delta"` is the related mode for an unlocked cursor: it moves
relatively but stays inside the viewport.

## Touch games

A mobile game usually feature-detects touch and never runs its touch code path
on a plain desktop Chrome, where `navigator.maxTouchPoints` is 0 and
`'ontouchstart' in window` is false. Turn the page into a touch device first:

```json
{
  "actions": [{
    "action": "touch_emulation",
    "session_id": "mobile",
    "enabled": true,
    "max_touch_points": 5
  }]
}
```

`'ontouchstart'` is decided while the document loads, so the page is reloaded by
default (`reload_page=true`). **That reload resets the session's render mode to
`normal` and drops every held key**, so enable touch emulation *before* engaging
the gate, not after.

Then send touch input — `tap`, `press`, `move`, `release`, `swipe`, or `cancel`,
with up to ten simultaneous points:

```json
{
  "actions": [
    {"action": "touch", "touch_action": "tap", "session_id": "mobile",
     "points": [{"x": 160, "y": 420}]},
    {"action": "touch", "touch_action": "swipe", "session_id": "mobile",
     "points": [{"x": 120, "y": 300, "end_x": 420, "end_y": 300}],
     "steps": 12, "duration_seconds": 0.25}
  ]
}
```

Each point takes `x`, `y`, an optional `id` for multi-finger gestures, and
`end_x`/`end_y` for a swipe. Like every other input action, a touch releases one
frame in step mode.

Fingers are tracked per session and lifted individually: `release` with
`points=[{"id": 1}]` ends that finger and leaves the others down, while `release`
with no points ends them all. A `tap` or `swipe` no longer disturbs a finger that
is already pressed — every one of those used to end the whole gesture, after
which Chrome refused to move the survivors with "Must send a TouchStart first".
The result reports `active_touches`, and a point outside the viewport is refused
rather than accepted and dropped.

## Watching what happens

| Call | What it tells you |
| --- | --- |
| `web_info(topic="game_probe")` | Canvas and WebGL context, iframe surfaces, document focus, `animation.fps` (or `animation.animation_suspended` while gated), load time, the console warnings and errors new since the previous probe, and which keys and buttons the session is still holding. |
| `web_info(topic="screenshot")` | A PNG of the current frame. In step mode it shows the last *released* frame, so take it after the step, never before. |
| `web_info(topic="console")` | `console.log/info/warn/error`, uncaught exceptions, and rejections with stack frames. Filter with `levels`, `kinds`, `contains`. |
| `web_info(topic="page_text")` | The DOM status line or HUD many games render outside the canvas. |

```json
{"topic":"console","params":{"session_id":"game","levels":["error"],"limit":20}}
```

The console buffer is armed when the session takes its tab, before the first
navigation, so an error thrown while the game boots is captured rather than lost —
which matters for a WebGL context that fails at startup and never logs again. The
in-page hook is registered through `Page.addScriptToEvaluateOnNewDocument` before
the session navigates anywhere, in the Selenium-backed modes exactly as in
`current`, so it runs ahead of each document's own first script and keeps doing so
across every later navigation. It used to be installed the first time the `console`
topic was read, which cost a Selenium session its first page's boot output and made
"reload and read again" the standard advice. Do not: the reload destroys the run
you were debugging, and the lines you wanted are already in the buffer.

The one exception is `attach_tab` — a claimed tab is recorded from the claim
onwards, and what it logged before that was recorded by nobody. Uncaught exceptions
reach Chrome's browser log either way. `game_probe` and the `console` topic read
from the same buffers, each keeping its own place in them; neither steals the
other's entries.

### Reading the console between probes

A game loop probes often, and a probe that repeated every warning the session
had ever logged became useless within a minute. So `game_probe` reports only
what is new since **its own** previous call, and says so in the result:

```json
{"console_scope": "new since the previous game_probe call"}
```

What follows from that:

- polling `game_probe` every frame is safe. The output stays the size of what
  just happened, not the size of the run;
- an entry is handed over exactly once. A probe result you discard takes its
  `console_messages` with it, so read them on every call — or reach for the
  `console` topic, which pages through history with `since_seq` and `limit` and
  can re-read a window. One probe also carries at most the hundred most recent
  of them, counted before the warning/error filter, so a burst larger than that
  loses its oldest lines;
- the two readers are independent. Polling the probe hides nothing from
  `console`, and reading `console` hides nothing from the probe;
- both channels are covered: the probe reads the in-page hook as well as
  Chrome's browser log, so `console.warn`, `console.error`, and uncaught
  exceptions arrive in `console_messages` whichever backend drives the session.
  In companion `current` mode Chrome's browser log is not readable at all, and
  earlier builds — which read only it — reported nothing there.

The probe still filters to `warn` and `error`. Use the `console` topic for
`log`/`info`, for stack frames, and for anything you want to read twice.

## Cleanup is not optional

Two things outlive a finished run and confuse everything that comes after:

```json
{
  "actions": [
    {"action": "release_inputs", "session_id": "game"},
    {"action": "render", "mode": "normal", "session_id": "game"}
  ]
}
```

- `release_inputs` lifts every key, mouse button and touch point the session is
  holding, and reports `held_keys: []`, `held_buttons: []` and `held_touches: []`.
  A held arrow key left behind keeps steering the next page, and a finger left
  down makes the next `move` fail with "Must send a TouchStart first". In step
  mode this call, too, releases one frame;
- `render` back to `normal` restores the real clocks, hands queued timers back to
  Chrome's scheduler with their remaining delay intact, and lets the page run on
  its own again. A page left in `step` mode looks frozen to the user.

Navigating the same session releases held input — it belonged to the old
document — and re-arms the gate on the new one, reporting `render_mode` and
`render_mode_restored`. That is a safety net for a level change, not a substitute
for cleaning up when you are done.

When an `input` batch fails part way through, `held_keys` deliberately reports
*more* than it can prove. Dispatch is not atomic — the bridge sends one event per
round trip — so once it has failed, any of the batch's keys may be physically
down. The books assume they all are, because a key the page holds and the session
has forgotten cannot be reached by `release_inputs` or by anything else, and
every later keystroke silently carries it. So a failed batch that reports three
held keys may really be holding none, and `release_inputs` is the cure either
way: releasing a key that was never pressed costs nothing. This is the deliberate
opposite of the old behaviour, which under-reported and left keys down with
nothing able to lift them.

## What is verified, and what is not

Verified by the deterministic test suite (`tests/test_game_playthrough.py`,
`tests/test_browser.py`, `tests/test_input_extras.py`,
`tests/test_input_defects.py`), with no network access:

- the level is finished with the same ticks, jumps, and deaths on every run:
  one released frame is exactly one physics tick;
- the virtual clock does not move between released frames, and moves by exactly
  `frame_delta_ms` per frame — including a custom delta;
- gated `setTimeout`/`setInterval` fire only when a frame is released, and are
  handed back to the real scheduler with their remaining delay when the gate
  lifts;
- a tapped key is still down while the frame runs, so the jump actually happens;
- the gate is reinstalled after the document is replaced;
- a game inside an offset, bordered iframe is stepped and hit by the pointer at
  the exact frame-local coordinate;
- a WebGL game is probed and played to its win condition.

Not a promise: **that a given model finishes a given game**. The mechanics above
are deterministic; the player is not. `scripts/live_agent_game.py` runs the whole
stack end to end — MCP over stdio, a local model in LM Studio, the platformer
fixture — and a 4B model does complete the level, but not on every attempt, and
it is not part of `pytest`:

```powershell
lms load qwen3.5-4b-mtp --context-length 16384 --parallel 1
python scripts/live_agent_game.py --model qwen3.5-4b-mtp
```

Public game sites also change without notice, which is why the frame gate, input
atomicity, and held-input recovery are verified against local fixtures
(`tests/fixtures/games/`: `platformer.html`, `iframe_host.html`, `webgl.html`,
`pointer.html`, `frame_transforms.html` for the scaled, rotated, and nested frames
the coordinate map has to survive, and `timers.html` for the gated clock) rather
than against a live portal.
