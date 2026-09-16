"""Getting past a captcha: see it, wait it out, or hand it to a solving service.

Detection already exists in ``browser_tools`` and is the honest part - it knows a
live widget from an article about widgets. What is missing is what to *do* next,
and there are only two real answers. Waiting is the default because it always
works and costs nothing: the browser is on the user's own screen, so a human can
click the box and the automation carries on where it stopped. A solving service
is the other answer, used only when the caller asks for it with mode='solve' (or
the operator sets WEB_SEARCH_NEO_CAPTCHA_AUTO_SOLVE=1) and a key is configured,
because it costs money per solve and belongs to a third party.

The token a service returns is not a click. It is a string the page's own script
was going to receive, and it only takes effect once the page is told - hence
``apply_token``, which fills the field every vendor hides for exactly this and
then calls the callback the widget registered.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable
from urllib import error, request

from web_search_neo.actions.waits import clamp_wait

# A human solver needs tens of seconds; the ceiling is the shared wait cap.
MIN_SOLVE_SECONDS = 30.0

# Vendor -> the sitekey attribute and the hidden field its token belongs in.
# reCAPTCHA and hCaptcha both hide a textarea; Turnstile uses an input. The
# names are fixed by the vendors, not by us. ``widget`` is the element that
# carries ``data-callback`` - the page's own function to hand the token to.
_VENDORS = {
    "recaptcha": {
        "detect": "div.g-recaptcha[data-sitekey], iframe[src*='recaptcha/api2'], iframe[src*='recaptcha/enterprise']",
        "widget": "div.g-recaptcha[data-callback]",
        "field": "textarea#g-recaptcha-response, textarea[name='g-recaptcha-response']",
        "task": "RecaptchaV2TaskProxyless",
        "enterprise_task": "RecaptchaV2EnterpriseTaskProxyless",
    },
    "hcaptcha": {
        "detect": "div.h-captcha[data-sitekey], iframe[src*='hcaptcha.com']",
        "widget": "div.h-captcha[data-callback]",
        "field": "textarea[name='h-captcha-response'], textarea#h-captcha-response",
        "task": "HCaptchaTaskProxyless",
    },
    "turnstile": {
        "detect": "div.cf-turnstile[data-sitekey], iframe[src*='challenges.cloudflare.com']",
        "widget": "div.cf-turnstile[data-callback]",
        "field": "input[name='cf-turnstile-response']",
        "task": "TurnstileTaskProxyless",
    },
}

MODES = ("detect", "wait", "solve", "auto")
# Opting a machine back into "a configured key is consent to spend it".
AUTO_SOLVE_ENV = "WEB_SEARCH_NEO_CAPTCHA_AUTO_SOLVE"

# Read the vendor, its sitekey and the page URL in one round trip: a solver needs
# all three, and asking for them separately invites them to disagree after a
# reload. The sitekey lives on the widget element, or on the iframe URL when the
# widget was rendered by script and left no element behind. reCAPTCHA Enterprise
# shares the widget markup with v2, so it is told apart by its frame or script;
# a token minted for the wrong product is refused by the site.
IDENTIFY_SCRIPT = """
const vendors = %s;
for (const [name, spec] of Object.entries(vendors)) {
  const element = document.querySelector(spec.detect);
  if (!element) continue;
  let sitekey = element.getAttribute && element.getAttribute('data-sitekey');
  if (!sitekey && element.tagName === 'IFRAME') {
    const match = /[?&]k=([^&]+)/.exec(element.src || '');
    if (match) sitekey = decodeURIComponent(match[1]);
  }
  if (!sitekey) {
    const holder = document.querySelector('[data-sitekey]');
    if (holder) sitekey = holder.getAttribute('data-sitekey');
  }
  let enterprise = false;
  if (name === 'recaptcha') {
    enterprise = !!(document.querySelector(
      "iframe[src*='recaptcha/enterprise'], script[src*='recaptcha/enterprise']")
      || (window.grecaptcha && window.grecaptcha.enterprise));
  }
  const task = enterprise ? (spec.enterprise_task || null) : spec.task;
  return {vendor: name, sitekey: sitekey || null, url: location.href, task: task,
          enterprise: enterprise};
}
return {vendor: null, sitekey: null, url: location.href, task: null, enterprise: false};
""" % json.dumps(_VENDORS)

# Put the token where the page expects it and then tell the page. Writing the
# field alone is not enough on any modern widget: the site's own code runs in the
# callback, and a form submitted without it is refused with the field populated.
# The callback is the one the widget names in data-callback, resolved as a plain
# property path on window - never evaluated - and called only if it is a
# function. Nothing is written when the page is no longer the one the token was
# minted for: a token is bound to a URL, and a navigated tab is another form.
APPLY_TOKEN_SCRIPT = """
const vendors = %s;
const info = arguments[0];
const token = arguments[1];
const expectedUrl = arguments[2];
const spec = vendors[info];
if (!spec) return {applied: false, reason: 'unknown vendor ' + info};
if (expectedUrl && location.href !== expectedUrl) {
  return {applied: false, fields: 0, callbacks: 0, url: location.href,
          reason: 'the page changed since the captcha was found'};
}
const fields = document.querySelectorAll(spec.field);
for (const field of fields) {
  field.value = token;
  field.dispatchEvent(new Event('input', {bubbles: true}));
  field.dispatchEvent(new Event('change', {bubbles: true}));
}
// A dotted name (obj.onSolved) is a method: it is called on its owner, so
// `this` inside it is the object it was registered on.
const resolve = (path) => {
  if (!/^[A-Za-z_$][\\w$]*(\\.[A-Za-z_$][\\w$]*)*$/.test(path || '')) return null;
  let owner = window;
  let target = window;
  for (const part of path.split('.')) {
    if (target == null || !(part in Object(target))) return null;
    owner = target;
    target = target[part];
  }
  return typeof target === 'function' ? {fn: target, owner: owner} : null;
};
let called = 0;
const named = [];
try {
  for (const widget of document.querySelectorAll(spec.widget)) {
    const name = widget.getAttribute('data-callback');
    const callback = resolve(name);
    if (callback && !named.includes(name)) {
      named.push(name); callback.fn.call(callback.owner, token); called++;
    }
  }
  if (!called && info === 'recaptcha' && window.___grecaptcha_cfg) {
    for (const client of Object.values(window.___grecaptcha_cfg.clients || {})) {
      for (const branch of Object.values(client || {})) {
        for (const leaf of Object.values(branch || {})) {
          if (leaf && typeof leaf.callback === 'function') { leaf.callback(token); called++; }
        }
      }
    }
  }
} catch (error) { /* a vendor that changed its internals is not a failure here */ }
return {applied: fields.length > 0 || called > 0, fields: fields.length, callbacks: called,
        callback_names: named};
""" % json.dumps(_VENDORS)


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"captcha mode must be detect, wait, solve, or auto, not '{mode}'")
    return mode


def auto_solve_enabled() -> bool:
    """Whether ``auto`` may spend money: only when the operator said so explicitly."""
    return (os.getenv(AUTO_SOLVE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def wants_paid_solve(mode: str, found: dict[str, Any]) -> bool:
    """A paid solve happens only on ``mode='solve'``, or ``auto`` with the opt-in env.

    A configured key is not consent by itself: one machine runs many agents,
    and a charge per page must never be the side effect of a default.
    """
    if mode == "solve":
        return True
    return (
        mode == "auto"
        and auto_solve_enabled()
        and solver_config()["configured"]
        and bool(found.get("vendor") and found.get("sitekey"))
    )


def solve_and_apply(
    found: dict[str, Any],
    run_script: Callable[[str, list[Any]], dict[str, Any]],
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    """Buy a token for the identified widget and hand it to the page.

    ``run_script(script, args)`` is the caller's execute_js. Success means the
    page took the token - a field was written or a callback ran - on the very
    URL the captcha was found on, not merely that the script returned.
    """
    vendor, sitekey = found.get("vendor"), found.get("sitekey")
    if not vendor or not sitekey:
        raise ValueError(
            "This captcha exposes no sitekey, so no service can be asked for a "
            "token; it has to be cleared in the page with mode='wait'."
        )
    if not found.get("task"):
        raise ValueError(
            f"No solving task is known for this {vendor} variant; clear it in the "
            "page with mode='wait'."
        )
    budget, note = clamp_wait(timeout_seconds, floor=MIN_SOLVE_SECONDS)
    solved = solve_remotely(found["task"], sitekey, found["url"], budget, poll_seconds)
    applied = run_script(APPLY_TOKEN_SCRIPT, [vendor, solved["token"], found["url"]])
    value = applied.get("value") if isinstance(applied.get("value"), dict) else {}
    ok = bool(applied.get("success")) and value.get("applied") is True
    result: dict[str, Any] = {
        "success": ok,
        "captcha_present": True,
        "mode": "solve",
        "vendor": vendor,
        "enterprise": bool(found.get("enterprise")),
        "applied": applied.get("value"),
        "cost": solved.get("cost"),
    }
    if note:
        result["timeout_note"] = note
    if ok:
        result["note"] = (
            "The token is in the page. Submitting the form is still the "
            "caller's move: some sites submit from the widget callback and "
            "some wait for the button."
        )
    else:
        reason = value.get("reason") or applied.get("error") or "no token field or callback was found"
        result["error"] = (
            f"The service returned a token but the page did not take it: {reason}. "
            "The solve was paid for; run detect again before retrying."
        )
    return result


def solver_config() -> dict[str, Any]:
    """The configured solving service, or the reason there is none.

    One key, one provider. 2captcha and anti-captcha speak the same JSON API, so
    the only thing that varies is the host, and pointing the host somewhere else
    is how a self-hosted or in-house solver gets used without new code here.
    """
    key = (os.getenv("WEB_SEARCH_NEO_CAPTCHA_KEY") or "").strip()
    host = (os.getenv("WEB_SEARCH_NEO_CAPTCHA_HOST") or "api.2captcha.com").strip()
    return {"configured": bool(key), "key": key, "host": host}


def _post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    call = request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(call, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except (error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"captcha service unreachable: {type(exc).__name__}: {exc}") from None


def solve_remotely(
    task_type: str,
    sitekey: str,
    page_url: str,
    timeout_seconds: float = 180.0,
    poll_seconds: float = 5.0,
) -> dict[str, Any]:
    """Ask the configured service for a token, polling until it answers or time runs out.

    Solves are slow by nature - a human on the other end takes tens of seconds -
    so the wait is long and the polling is unhurried. The cost of a solve is real
    money, which is why nothing here runs unless a key was deliberately set.
    """
    config = solver_config()
    if not config["configured"]:
        raise ValueError(
            "No captcha service is configured. Set WEB_SEARCH_NEO_CAPTCHA_KEY to "
            "use one, or solve the captcha by hand with mode='wait'."
        )
    base = f"https://{config['host']}"
    created = _post(
        f"{base}/createTask",
        {
            "clientKey": config["key"],
            "task": {"type": task_type, "websiteURL": page_url, "websiteKey": sitekey},
        },
        timeout=30.0,
    )
    if created.get("errorId"):
        raise RuntimeError(f"captcha service refused the task: {created.get('errorDescription')}")
    task_id = created.get("taskId")
    if not task_id:
        raise RuntimeError(f"captcha service accepted the request but returned no taskId: {created}")
    budget, _ = clamp_wait(timeout_seconds, floor=MIN_SOLVE_SECONDS)
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        time.sleep(max(1.0, float(poll_seconds)))
        result = _post(
            f"{base}/getTaskResult", {"clientKey": config["key"], "taskId": task_id}, timeout=30.0
        )
        if result.get("errorId"):
            raise RuntimeError(f"captcha service failed the task: {result.get('errorDescription')}")
        if result.get("status") == "ready":
            solution = result.get("solution") or {}
            token = (
                solution.get("gRecaptchaResponse")
                or solution.get("token")
                or solution.get("text")
            )
            if not token:
                raise RuntimeError(f"captcha service returned no token: {solution}")
            return {"token": str(token), "task_id": task_id, "cost": result.get("cost")}
    raise RuntimeError(
        f"captcha service did not solve the task within {budget:.0f}s (task {task_id})"
    )
