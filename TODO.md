# TODO

- Add an approved, provider-supported automatic CAPTCHA completion integration if a legal and reliable option becomes available. Automatic CAPTCHA bypass is intentionally not implemented. Current behavior defaults to immediate provider fallback; opt-in manual mode opens a visible browser for up to three minutes and falls back if the challenge remains.
- Add virtual gamepad input on top of the current keyboard, pointer, wheel, and touch coverage, so engines that only read the Gamepad API can be driven.
- Reach into closed shadow roots. The registry already counts them as `closed_shadow_roots`, but the outline cannot describe or address their contents.
- Offer `Emulation.setVirtualTimePolicy` as a stricter determinism mode next to the current JavaScript gate. The gate patches `performance.now()`, `Date.now()`, and the timer APIs from inside the page; the CDP policy would also cover code paths the page-level patch cannot reach.
- Resolve `ref:N` and piercing locators inside nested iframes for actions, not only for inspection. The outline already descends into nested same-origin frames, but element handles are resolved from the document the session is switched to.

Done and kept here only as a record of scope:

- ~~Add iframe and open Shadow DOM traversal to rendered page inspection.~~ `page_outline`, `page_text`, and `find` walk open shadow roots and same-origin iframes, and `ref:N` plus `#host >>> .inner` locators address elements through them.
- ~~Add native touch gestures on top of the canvas/WebGL keyboard, pointer, drag, FPS, and console probes.~~ The `touch` action covers tap, press, move, release, swipe, and cancel with up to ten points, and `touch_emulation` makes a page report itself as a touch device.
