"""Audit helper: verify every internal link, path reference and anchor in the docs."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(r"C:\Git\PythonUrlFeatch")
DOCS = [
    ROOT / "README.md",
    ROOT / "INSTALL.md",
    ROOT / "TODO.md",
    ROOT / "docs" / "complex-forms.md",
    ROOT / "docs" / "playing-games.md",
    ROOT / "skills" / "web-search-neo" / "SKILL.md",
]

LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMG = re.compile(r'<img[^>]+src="([^"]+)"')
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.M)
CODEPATH = re.compile(r"`([A-Za-z0-9_\-./\\]+\.(?:py|md|json|ps1|html|js|yaml|yml|txt|log))`")
BACKTICK_DIR = re.compile(r"`((?:tests|docs|scripts|skills|chrome-extension)[A-Za-z0-9_\-./\\]*)`")


def slug(text: str) -> str:
    text = re.sub(r"`", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text)


anchors: dict[Path, set[str]] = {}
for doc in DOCS:
    body = doc.read_text(encoding="utf-8")
    anchors[doc] = {slug(m.group(2)) for m in HEADING.finditer(body)}

print("=== LINK CHECK ===")
for doc in DOCS:
    body = doc.read_text(encoding="utf-8")
    lines = body.splitlines()
    for m in list(LINK.finditer(body)) :
        target = m.group(2)
        line_no = body[: m.start()].count("\n") + 1
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.startswith("#"):
            want = target[1:].lower()
            if want not in anchors[doc]:
                print(f"MISSING ANCHOR  {doc.name}:{line_no}  [{m.group(1)}]({target})")
            continue
        path_part, _, frag = target.partition("#")
        resolved = (doc.parent / path_part).resolve()
        if not resolved.exists():
            print(f"MISSING FILE    {doc.name}:{line_no}  {target}")
            continue
        if frag:
            other = anchors.get(resolved)
            if other is None and resolved.suffix == ".md":
                other = {slug(x.group(2)) for x in HEADING.finditer(resolved.read_text(encoding='utf-8'))}
            if other is not None and frag.lower() not in other:
                print(f"MISSING ANCHOR  {doc.name}:{line_no}  {target}")
    for m in IMG.finditer(body):
        src = m.group(1)
        if src.startswith("http"):
            continue
        line_no = body[: m.start()].count("\n") + 1
        if not (doc.parent / src).resolve().exists():
            print(f"MISSING IMAGE   {doc.name}:{line_no}  {src}")

print("\n=== BACKTICKED PATH CHECK (files) ===")
seen = set()
for doc in DOCS:
    body = doc.read_text(encoding="utf-8")
    for m in list(CODEPATH.finditer(body)) + list(BACKTICK_DIR.finditer(body)):
        raw = m.group(1)
        line_no = body[: m.start()].count("\n") + 1
        key = (doc.name, raw)
        if key in seen:
            continue
        seen.add(key)
        cand = (ROOT / raw.replace("\\", "/")).resolve()
        if not cand.exists():
            print(f"NOT FOUND       {doc.name}:{line_no}  {raw}")
