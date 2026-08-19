"""Getting past a captcha: see it, wait it out, or hand it to a solving service.

Detection already exists in ``browser_tools`` and is the honest part - it knows a
live widget from an article about widgets. What is missing is what to *do* next,
and there are only two real answers. Waiting is the default because it always
works and costs nothing: the browser is on the user's own screen, so a human can
click the box and the automation carries on where it stopped. A solving service
is the other answer, used only when a key is configured, because it costs money
per solve and belongs to a third party.

The token a service returns is not a click. It is a string the page's own script
was going to receive, and it only takes effect once the page is told - hence
``apply_token``, which fills the field every vendor hides for exactly this and
then calls the callback the widget registered.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib import error, request

# Vendor -> the sitekey attribute and the hidden field its token belongs in.
# reCAPTCHA and hCaptcha both hide a textarea; Turnstile uses an input. The
# names are fixed by the vendors, not by us.
_VENDORS = {
    "recaptcha": {
        "detect": "div.g-recaptcha[data-sitekey], iframe[src*='recaptcha/api2'], iframe[src*='recaptcha/enterprise']",
        "field": "textarea#g-recaptcha-response, textarea[name='g-recaptcha-response']",
        "task": "RecaptchaV2TaskProxyless",
    },
    "hcaptcha": {
        "detect": "div.h-captcha[data-sitekey], iframe[src*='hcaptcha.com']",
        "field": "textarea[name='h-captcha-response'], textarea#h-captcha-response",
        "task": "HCaptchaTaskProxyless",
    },
    "turnstile": {
        "detect": "div.cf-turnstile[data-sitekey], iframe[src*='challenges.cloudflare.com']",
        "field": "input[name='cf-turnstile-response']",
        "task": "TurnstileTaskProxyless",
    },
}

# Read the vendor, its sitekey and the page URL in one round trip: a solver needs
# all three, and asking for them separately invites them to disagree after a
# reload. The sitekey lives on the widget element, or on the iframe URL when the
# widget was rendered by script and left no element behind.
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
  return {vendor: name, sitekey: sitekey || null, url: location.href, task: spec.task};
}
return {vendor: null, sitekey: null, url: location.href, task: null};
""" % json.dumps(_VENDORS)

# Put the token where the page expects it and then tell the page. Writing the
# field alone is not enough on any modern widget: the site's own code runs in the
# callback, and a form submitted without it is refused with the field populated.
APPLY_TOKEN_SCRIPT = """
const vendors = %s;
const info = arguments[0];
const token = arguments[1];
const spec = vendors[info];
if (!spec) return {applied: false, reason: 'unknown vendor ' + info};
const fields = document.querySelectorAll(spec.field);
for (const field of fields) {
  field.value = token;
  field.dispatchEvent(new Event('input', {bubbles: true}));
  field.dispatchEvent(new Event('change', {bubbles: true}));
}
let called = 0;
try {
  if (info === 'recaptcha' && window.___grecaptcha_cfg) {
    for (const client of Object.values(window.___grecaptcha_cfg.clients || {})) {
      for (const branch of Object.values(client || {})) {
        for (const leaf of Object.values(branch || {})) {
          if (leaf && typeof leaf.callback === 'function') { leaf.callback(token); called++; }
        }
      }
    }
  }
  if (info === 'hcaptcha' && typeof window.hcaptchaOnLoad === 'function') {
    window.hcaptchaOnLoad(token); called++;
  }
} catch (error) { /* a vendor that changed its internals is not a failure here */ }
return {applied: fields.length > 0, fields: fields.length, callbacks: called};
""" % json.dumps(_VENDORS)


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
    deadline = time.monotonic() + max(30.0, float(timeout_seconds))
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
        f"captcha service did not solve the task within {timeout_seconds:.0f}s (task {task_id})"
    )
