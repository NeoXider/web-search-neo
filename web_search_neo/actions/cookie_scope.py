"""Cookie scoping and the filtered clear, kept apart from the browser facade.

``cookies clear`` must never be a mass delete in disguise. Chrome's own
``Storage.clearCookies`` has no filter at all and wipes every cookie of the
profile - in current Chrome, every login of the user - so a clear here needs a
domain, deletes exactly the cookies that match it one by one, and counts what is
gone from a fresh read. A domain that is really a registry (``com``,
``co.uk``, ``github.io``), or a filter that reaches more than a handful of
different hosts, needs ``confirm_clear_all`` like the unfiltered clear.

The public-suffix test prefers a real Public Suffix List when one is already
installed (``tldextract`` with its bundled snapshot, or ``publicsuffix2``) - the
project takes no new dependency for it - and otherwise falls back to a built-in
list of the suffixes that matter in practice. The fallback errs the safe way: a
name without a dot, or a two-letter country code under a generic second level
(``co.jp``, ``com.br``), counts as a suffix too.
"""
from __future__ import annotations

import json
from typing import Any

# Registrable-looking names that are really registries: second-level country
# registries plus hosting platforms where every subdomain is somebody else's site.
_KNOWN_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
    "com.br", "net.br", "org.br", "gov.br",
    "com.cn", "net.cn", "org.cn", "gov.cn",
    "co.in", "net.in", "org.in", "gov.in",
    "co.nz", "net.nz", "org.nz", "co.za", "org.za",
    "com.mx", "com.tr", "com.sg", "com.hk", "com.tw", "com.ar", "com.pl",
    "com.ua", "kiev.ua", "in.ua", "pp.ua", "org.ua", "net.ua",
    "com.ru", "net.ru", "org.ru", "msk.ru", "spb.ru",
    "ca.us", "ny.us", "eu.org",
    "github.io", "gitlab.io", "herokuapp.com", "vercel.app", "netlify.app", "pages.dev",
    "web.app", "firebaseapp.com", "appspot.com", "azurewebsites.net", "cloudfront.net",
    "amazonaws.com", "s3.amazonaws.com", "workers.dev", "onrender.com", "fly.dev",
    "ngrok.io", "ngrok-free.app", "ngrok.app", "myshopify.com",
})
_GENERIC_SECOND_LEVELS = frozenset({"co", "com", "net", "org", "gov", "edu", "ac", "ne", "or", "go"})
# More distinct hosts than this under one filter is a sweep, not a site.
MAX_HOSTS_WITHOUT_CONFIRMATION = 5


def _library_says(name: str) -> bool | None:
    """The installed Public Suffix List's answer, or None when none is installed."""
    try:
        import tldextract  # type: ignore[import-not-found]

        # Private domains (github.io, blogspot.com) are exactly the registries that matter here.
        extracted = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)(name)
        return not extracted.domain and bool(extracted.suffix)
    except Exception:
        pass  # not installed, or its snapshot failed: try the next list
    try:
        import publicsuffix2  # type: ignore[import-not-found]

        return publicsuffix2.get_tld(name, strict=True) == name
    except ImportError:
        return None
    except Exception:
        return None


def is_public_suffix(domain: str) -> bool:
    """True when ``domain`` (leading dot ignored) is a TLD or a public suffix."""
    name = str(domain or "").strip().lstrip(".").lower().rstrip(".")
    if name == "localhost" or name.replace(".", "").isdigit():
        return False  # one machine, not a registry
    if not name or "." not in name:
        return True
    if name in _KNOWN_SUFFIXES or name.split(".", 1)[0] == "blogspot":
        return True
    labels = name.split(".")
    if len(labels) == 2 and labels[0] in _GENERIC_SECOND_LEVELS and len(labels[1]) == 2:
        return True
    return bool(_library_says(name))


def in_domain(cookie: dict[str, Any], domain: str) -> bool:
    """The domain itself or a subdomain of it - never a mere substring."""
    wanted = str(domain or "").lstrip(".").lower()
    have = str(cookie.get("domain") or "").lstrip(".").lower()
    return have == wanted or have.endswith("." + wanted)


def _identity(cookie: dict[str, Any]) -> tuple[Any, ...]:
    return (cookie.get("name"), cookie.get("domain"), cookie.get("path"),
            json.dumps(cookie.get("partitionKey"), sort_keys=True))


def _jar(driver: Any) -> list[dict[str, Any]]:
    return (driver.execute_cdp_cmd("Storage.getCookies", {}) or {}).get("cookies") or []


def clear(
    driver: Any, session_id: str, domain: str | None, name: str | None, confirm_clear_all: bool
) -> dict[str, Any]:
    """Delete exactly the cookies ``domain``/``name`` match, or refuse a sweep."""
    if not domain and not confirm_clear_all:
        # A name alone ("sid", "session") exists on hundreds of sites.
        raise ValueError(
            "cookies clear needs a domain: without one it would delete "
            + (f"every '{name}' cookie of every site" if name else
               "every cookie of this browser profile (every site's login)")
            + ". Pass domain, or confirm_clear_all=true if that is really what is wanted."
        )
    if not domain and not name:
        driver.execute_cdp_cmd("Storage.clearCookies", {})
        return {"success": True, "session_id": session_id, "cleared": "all"}
    if domain and not confirm_clear_all and is_public_suffix(domain):
        raise ValueError(
            f"cookies clear domain='{domain}' names a top-level domain or public suffix, so it "
            "would delete the cookies of every site under it. Pass the site's own domain "
            "(example.co.uk, not co.uk), or confirm_clear_all=true if that is really wanted."
        )
    matched = [c for c in _jar(driver) if (not name or c.get("name") == name)
               and (not domain or in_domain(c, domain))]
    hosts = {str(c.get("domain") or "").lstrip(".").lower() for c in matched}
    if len(hosts) > MAX_HOSTS_WITHOUT_CONFIRMATION and not confirm_clear_all:
        raise ValueError(
            f"cookies clear domain='{domain}' matches cookies of {len(hosts)} different hosts "
            f"(more than {MAX_HOSTS_WITHOUT_CONFIRMATION}), which is a sweep rather than one "
            "site. Narrow the domain, or pass confirm_clear_all=true."
        )
    for c in matched:
        # A partitioned (CHIPS) cookie is only deleted with its partition.
        driver.execute_cdp_cmd("Network.deleteCookies", {
            "name": c.get("name"), "domain": c.get("domain"), "path": c.get("path") or "/",
            **({"partitionKey": c["partitionKey"]} if c.get("partitionKey") else {}),
        })
    left = {_identity(c) for c in _jar(driver)}  # counted from a fresh read
    gone = [c for c in matched if _identity(c) not in left]
    stayed = [c for c in matched if _identity(c) in left]
    return {"success": not stayed, "session_id": session_id, "deleted": len(gone),
            "deleted_cookies": [{k: c.get(k) for k in ("name", "domain", "path")} for c in gone],
            **({"not_deleted": [{k: c.get(k) for k in ("name", "domain", "path", "partitionKey")}
                                for c in stayed]} if stayed else {})}
