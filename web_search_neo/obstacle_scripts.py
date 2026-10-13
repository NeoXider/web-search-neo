"""Страничные скрипты auto_fix: прочитать препятствие, отметить чекбокс согласия.

Вынесено из audit_actions.py (файл с ограниченным размером) без изменения
поведения. Скрипты читают только то, что страница назвала сама: live-область,
которая просит подождать, подписи неотмеченных чекбоксов, подсказку формата рядом
с полем. Ни одного угаданного селектора: адрес контроля строится самим DOM'ом.

Значения в отчёт не копируются — только подписи, адреса контролей и форма ответа.
"""

from __future__ import annotations

import json
from typing import Any

# Адрес контроля так, как его увидел бы человек: id, name, иначе короткий путь.
SELECTOR_HELPER = """
function browserSelector(node) {
  if (!node || node.nodeType !== 1) return '';
  if (node.id) return '#' + node.id;
  const name = node.getAttribute('name');
  if (name && ['INPUT', 'SELECT', 'TEXTAREA'].includes(node.tagName)) {
    return `${node.tagName.toLowerCase()}[name="${name}"]`;
  }
  const path = [];
  for (let cur = node; cur && cur.nodeType === 1 && path.length < 6; cur = cur.parentElement) {
    let part = cur.tagName.toLowerCase();
    if (typeof cur.className === 'string' && cur.className.trim()) {
      part += '.' + cur.className.trim().split(/\\s+/).slice(0, 2).join('.');
    }
    path.unshift(part);
  }
  return path.join(' > ');
}
"""

# Что страница сама сказала о своём препятствии.
READ_OBSTACLES = """
const words = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
const out = {waiting_for: '', consents: [], format_hints: []};
const ask = document.querySelector('[aria-live], [role=status], [role=alert], .error, .alert, .message');
if (ask) out.waiting_for = words(ask).slice(0, 300);
for (const box of document.querySelectorAll('input[type=checkbox]')) {
  if (box.checked) continue;
  const id = box.getAttribute('id');
  const byFor = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
  let label = box.labels && box.labels.length ? words(box.labels[0])
            : (byFor ? words(byFor) : words(box.closest('label') || box.parentElement || ''));
  if (!label) {
    const row = box.closest('div,li,p,section');
    label = row ? words(row).slice(0, 120) : '';
  }
  if (label) out.consents.push({label: label.slice(0, 160), selector: browserSelector(box)});
}
for (const field of document.querySelectorAll('input[aria-describedby], input[aria-invalid], input[data-format], .field-error')) {
  const describedBy = field.getAttribute('aria-describedby') || '';
  const hintNode = describedBy ? document.getElementById(describedBy.split(/\\s+/)[0]) : null;
  const text = hintNode ? words(hintNode)
                        : words(field.closest('.field,.form-group,dd,p') || (field.matches('input') ? '' : field));
  if (text && /format|pattern|example|must match/i.test(text)) {
    const input = field.matches('input') ? field : field.closest('input');
    const selector = browserSelector(input);
    if (selector) out.format_hints.push({label: text.slice(0, 200), selector: selector});
  }
}
return out;
"""

# Отметить ровно один чекбокс: клик по его собственной подписи, как сделал бы человек.
TICK_CONSENT = """
const node = document.querySelector(%s);
if (!node) return {success: false, error: 'the named control is not on the page'};
if (node.type !== 'checkbox') return {success: false, error: 'that control is not a checkbox'};
if (node.checked) return {success: true, already: true};
(node.labels && node.labels[0] ? node.labels[0] : node).click();
return {success: true, checked: Boolean(node.checked)};
"""


def read_script() -> str:
    """Читающий скрипт вместе с построителем адреса контроля."""
    return SELECTOR_HELPER + READ_OBSTACLES


def tick_script(selector: str) -> str:
    """Скрипт одного клика; адрес подставляется JSON'ом, а не склейкой."""
    return SELECTOR_HELPER + TICK_CONSENT % json.dumps(selector)


def state_of(value: Any) -> dict[str, Any]:
    """Ответ читающего скрипта: только списки подписей и адресов, всё остальное пусто."""
    if not isinstance(value, dict):
        return {}
    out = {"waiting_for": str(value.get("waiting_for") or "")[:300]}
    for key in ("consents", "format_hints"):
        items = value.get(key)
        out[key] = [item for item in (items if isinstance(items, list) else [])
                    if isinstance(item, dict) and str(item.get("selector") or "")]
    return out
