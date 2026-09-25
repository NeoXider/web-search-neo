"""Passive site checks for the owner of a site: security, performance, HAR, test runs.

Everything here reads what an ordinary browser visit already shows - response
headers, cookies, the rendered page, the certificate the TLS handshake presents,
``/.well-known/security.txt`` and ``/robots.txt``. Nothing probes, guesses,
enumerates or fuzzes: one URL, one ordinary page load, a handful of plain GETs
of well-known files, all listed in the report's ``requests_made``.

The modules are pure analysis over dicts; the MCP wrappers that open sessions and
read the browser live in ``web_search_neo/audit_actions.py``.
"""
