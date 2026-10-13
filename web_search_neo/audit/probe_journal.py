"""Дурнальная память разведки API: что уже пробовали и чем оно ответило.

Агент, идущий по API вслепую, вынужден помнить рукой, какой URL отдал
403-пусто, а какой — 404-JSON. Этот журнал ведёт инструмент сам: очень
маленькая долговечная память под каталогом загрузки harness'а
(``WEB_SEARCH_NEO_DOWNLOAD_DIR``, см. fetch.safety — нового места не
заводится). Ни одного нового запроса: сюда попадает только то, что
страница уже запросила сама.

Файл: ``<download_root>/probe_journal/<host>.json`` — один JSON на хост::

    {"host": ..., "version": 1, "entries": [
      {"method": "GET", "path": "/v1/users/{id}", "status": 403,
       "meaning": "access_scope", "note": "rejected_by=edge",
       "count": 3, "last_seen": "2026-10-13T...Z"}],
     "evicted": 0}

Никаких секретов и тел ответов: только метод, шаблон пути (id-сегменты
свёрнуты, query отрезана), статус, код смысла и счётчик повторов.
Повторы одной четвёрки (метод, путь, статус, смысл) сливаются в ``count``.
На хост — не больше MAX_ENTRIES_PER_HOST записей; вытесняются самые
старые, счётчик вытесненных честно хранится в ``evicted``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from web_search_neo.fetch.safety import download_root

JOURNAL_SUBDIR = "probe_journal"
JOURNAL_VERSION = 1
MAX_ENTRIES_PER_HOST = 200
NOTE_LIMIT = 160

_SAFE_HOST = re.compile(r"[^a-z0-9.\-]+")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def journal_dir(root: Path | str | None = None) -> Path:
    """Каталог журнала: подкаталог harness download/state-каталога."""
    base = Path(root).expanduser() if root is not None else download_root()
    return base / JOURNAL_SUBDIR


def host_of_url(url: str) -> str:
    """Хост URL строчными; '' для не-сетевых и мусорных адресов."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https", "ws", "wss"}:
        return ""
    return (parts.hostname or "").rstrip(".").lower()


def safe_host_file(host: str) -> str:
    """Имя файла под хост: только безопасные символы, порт через '_'."""
    name = _SAFE_HOST.sub("_", str(host or "").lower().replace(":", "_")).strip("._")
    return (name or "unknown-host")[:120]


def journal_file(host: str, root: Path | str | None = None) -> Path:
    """Путь JSON-журнала одного хоста."""
    return journal_dir(root) / f"{safe_host_file(host)}.json"


def _read(host: str, root: Path | str | None = None) -> dict[str, Any]:
    path = journal_file(host, root)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {"host": host, "version": JOURNAL_VERSION, "entries": [], "evicted": 0}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"host": host, "version": JOURNAL_VERSION, "entries": [], "evicted": 0}
    if not isinstance(data, dict):
        return {"host": host, "version": JOURNAL_VERSION, "entries": [], "evicted": 0}
    entries = data.get("entries")
    data["entries"] = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    try:
        data["evicted"] = int(data.get("evicted") or 0)
    except (TypeError, ValueError):
        data["evicted"] = 0
    data["host"] = host
    return data


def _write(host: str, data: dict[str, Any], root: Path | str | None = None) -> Path:
    path = journal_file(host, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"host": host, "version": JOURNAL_VERSION,
                          "entries": data["entries"], "evicted": data["evicted"]},
                         ensure_ascii=False, indent=1)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".journal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def meaning_code(meaning: Any) -> str:
    """Код смысла одной строкой: hint важнее class, иначе 'http_<status>'/''."""
    if isinstance(meaning, dict):
        for key in ("hint", "class"):
            value = meaning.get(key)
            if isinstance(value, str) and value:
                return value[:60]
        status = meaning.get("status")
        try:
            return f"http_{int(status)}" if status else ""
        except (TypeError, ValueError):
            return ""
    return str(meaning or "")[:60]


def record(method: str, url: str, status: int, meaning: Any = "",
           note: str = "", *, root: Path | str | None = None) -> dict[str, Any]:
    """Добавить попытку в журнал хоста; повторы сливаются в ``count`` (до 200/хост)."""
    from web_search_neo.audit.api_parts import path_template  # свой же пакет: граница разрешает

    host = host_of_url(url)
    if not host:
        return {"recorded": False, "why": "no_host"}
    try:
        status = int(status)
    except (TypeError, ValueError):
        return {"recorded": False, "why": "bad_status"}
    code = meaning_code(meaning)
    # Late import keeps the module import-light; mask_text is a pure helper.
    from web_search_neo.audit.api_parts import mask_text

    entry_note = mask_text(str(note or ""))[:NOTE_LIMIT]
    key = (str(method or "GET").upper(), path_template(str(url)), status, code)
    data = _read(host, root)
    for entry in data["entries"]:
        if (entry.get("method"), entry.get("path"), entry.get("status"),
                entry.get("meaning", "")) == key:
            try:
                entry["count"] = int(entry.get("count") or 1) + 1
            except (TypeError, ValueError):
                entry["count"] = 2
            entry["last_seen"] = _utcnow()
            if entry_note:
                entry["note"] = entry_note
            _write(host, data, root)
            return {"recorded": True, "merged": True, "host": host,
                    "entry": dict(entry)}
    data["entries"].append({"method": key[0], "path": key[1], "status": key[2],
                            "meaning": key[3], "note": entry_note,
                            "count": 1, "last_seen": _utcnow()})
    if len(data["entries"]) > MAX_ENTRIES_PER_HOST:
        data["entries"].sort(key=lambda e: str(e.get("last_seen") or ""))
        dropped = len(data["entries"]) - MAX_ENTRIES_PER_HOST
        data["entries"] = data["entries"][dropped:]
        data["evicted"] += dropped
    path = _write(host, data, root)
    return {"recorded": True, "merged": False, "host": host,
            "entry": dict(data["entries"][-1]), "file": str(path)}


def seed_from_traffic(rows: list[dict[str, Any]], bodies: dict[str, str] | None = None,
                      *, root: Path | str | None = None) -> dict[str, Any]:
    """Посеять журнал из уже захваченного трафика. Новых запросов — ноль.

    rows — строки сетевого журнала сессии, bodies — тела ошибок из памяти
    Chrome (оба уже есть у api_report). Пишутся только ответы с ошибкой
    (status >= 400): это и есть тупики, которые не стоит перепробовать.
    Примитивы — вызываемые аргументы не нужны: всё читается из переданных
    структур; сеть не трогается.
    """
    from web_search_neo.audit import api_errors as _errors

    recorded, hosts = 0, set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            status = int(row.get("status") or 0)
        except (TypeError, ValueError):
            continue
        if status < 400:
            continue
        url = str(row.get("url") or "")
        if not host_of_url(url):
            continue
        headers = row.get("headers") if isinstance(row.get("headers"), dict) else {}
        body = _errors.parse_body((bodies or {}).get(str(row.get("id") or "")))
        sense = _errors.error_meaning(status, body, headers)
        code = meaning_code(sense) or f"http_{status}"
        note = f"rejected_by={sense.get('rejected_by', 'unknown')}"
        if isinstance(body.get("code"), str) and body["code"]:
            note += f" code={body['code'][:40]}"
        out = record(str(row.get("method") or "GET"), url, status, code, note, root=root)
        if out.get("recorded"):
            recorded += 1
            hosts.add(out["host"])
    return {"recorded": recorded, "hosts": sorted(hosts)}


def query(host: str, *, root: Path | str | None = None,
          limit: int | None = None) -> dict[str, Any]:
    """Записанные попытки хоста, сгруппированные по исходу (коду смысла)."""
    name = str(host or "").lower().rstrip(".")
    data = _read(name, root)
    entries = sorted(data["entries"],
                     key=lambda e: (-int(e.get("count") or 1), str(e.get("path") or ""),
                                    str(e.get("method") or ""), -int(e.get("status") or 0)))
    total = sum(int(e.get("count") or 1) for e in entries)
    by_outcome: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_outcome.setdefault(str(entry.get("meaning") or "unclassified"), []).append(dict(entry))
    out: dict[str, Any] = {"host": name, "attempts": total,
                           "entries": [dict(e) for e in entries],
                           "by_outcome": by_outcome, "evicted": data["evicted"]}
    if limit is not None:
        clipped = out["entries"][:max(0, int(limit))]
        out["entries"] = clipped
        out["entries_omitted"] = max(0, len(entries) - len(clipped))
    return out
