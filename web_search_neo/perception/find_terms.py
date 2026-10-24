"""How ``find`` reads an agent's query, and what it hands back for a fact.

An agent asks the way a person talks: "link to Unreal Engine", "click the Save
button", "initial release date". Scored word for word, "to", "the" and "link"
counted as much as "Unreal Engine", so a long login link that happened to hold
"to" and "in" beat the one right answer, and a role word went looking for itself
in the text. This script splits the query first:

* filler words (articles, prepositions, "click"/"press") are dropped from the
  words that must match - the whole query is still scored too, so a button that
  really reads "Log in" keeps its exact match;
* role words ("link", "button", "field", "кнопка") become a soft role hint that
  lifts elements of that role, instead of text to find;
* a label the query covers ("Initial release" in "initial release date") counts
  as an answer, and a table header or a definition term hands back its value -
  the cell beside it - as ``answer``, so a fact on the page is one call away.

Concatenated after ``JS_LIBRARY`` (it needs ``wsnNorm``, ``wsnFieldScore``,
``wsnTokenMatch`` and ``wsnClean``) and before the find script that uses it.
"""

FIND_TERMS_JS = r"""
const WSN_FIND_STOPWORDS = new Set([
  'a', 'an', 'the', 'to', 'of', 'for', 'on', 'in', 'at', 'with', 'by', 'and', 'or', 'is',
  'are', 'this', 'that', 'my', 'your', 'its', 'me', 'please', 'from', 'about', 'into',
  'what', 'which', 'who', 'when', 'where', 'how', 'какой', 'какая', 'какие', 'когда', 'где', 'кто',
  'в', 'во', 'на', 'с', 'со', 'к', 'ко', 'по', 'для', 'и', 'или', 'о', 'об', 'от', 'до',
  'из', 'у', 'что', 'это', 'мне', 'пожалуйста',
  'click', 'press', 'tap', 'нажать', 'нажми', 'нажмите', 'кликни', 'клик', 'щелкни'
].map(wsnNorm));
const WSN_FIND_ROLE_WORDS = {};
for (const [roles, words] of [
  [['button'], ['button', 'btn', 'кнопка', 'кнопку']],
  [['link'], ['link', 'ссылка', 'ссылку']],
  [['textbox', 'searchbox', 'combobox'], ['field', 'input', 'textbox', 'поле']],
  [['textbox', 'searchbox', 'combobox', 'checkbox'], ['box']],
  [['checkbox', 'switch'], ['checkbox', 'чекбокс', 'галочка', 'галочку', 'флажок']],
  [['radio'], ['radio']],
  [['combobox', 'listbox'], ['dropdown', 'combobox', 'listbox']],
  [['tab'], ['tab', 'вкладка', 'вкладку']],
  [['heading'], ['heading', 'заголовок']],
  [['image'], ['image', 'картинка', 'картинку', 'изображение']],
  [['dialog'], ['dialog', 'modal']]
]) {
  for (const word of words) WSN_FIND_ROLE_WORDS[wsnNorm(word)] = roles;
}
// Facts live in table headers and definition lists, which no control selector
// reaches; find alone looks at them.
const WSN_FIND_SELECTOR = WSN_CANDIDATE_SELECTOR + ', th, dt, caption, figcaption, legend';

function wsnFindTerms(normalized) {
  const all = normalized ? normalized.split(' ') : [];
  const words = [];
  const roles = new Set();
  for (const token of all) {
    if (WSN_FIND_STOPWORDS.has(token)) continue;
    const hinted = WSN_FIND_ROLE_WORDS[token];
    if (hinted && all.length > 1) {
      for (const role of hinted) roles.add(role);
      continue;
    }
    words.push(token);
  }
  // A query that is nothing but filler ("About", "Click") is the label itself.
  if (!words.length) return {query: normalized, tokens: all, roles: new Set(), reduced: false};
  return {query: words.join(' '), tokens: words, roles: roles, reduced: words.length !== all.length};
}

function wsnFindScore(terms, normalized, allTokens, value, role) {
  let best = wsnFieldScore(normalized, allTokens, value);
  // The words alone, without the role word, are a weaker answer from an element
  // of another role: "search field" is not the Search button.
  const fit = !terms.roles.size || terms.roles.has(role) ? 0.97 : 0.75;
  if (terms.reduced) best = Math.max(best, fit * wsnFieldScore(terms.query, terms.tokens, value));
  const target = wsnNorm(value);
  if (!target) return best;
  const fieldTokens = target.split(' ').filter(token => !WSN_FIND_STOPWORDS.has(token));
  // A short label the query spells out in full: "Initial release" answers
  // "initial release date" even though "date" is not on it. A row header or a
  // term may be one word ("Release"); any other one-word label is too loose.
  const fact = role === 'rowheader' || role === 'term';
  if (fieldTokens.length >= (fact ? 1 : 2) && fieldTokens.length < terms.tokens.length
      && fieldTokens.every(field => terms.tokens.some(token => wsnTokenMatch(token, field)))) {
    best = Math.max(best, 20 + 16 * (fieldTokens.length / terms.tokens.length));
  }
  // Every query word somewhere in a long text is a weaker answer than in a label.
  if (best < 45 && fieldTokens.length > 8) {
    best *= 0.8 + 0.2 * Math.min(1, (terms.tokens.length + 2) / fieldTokens.length);
  }
  return best;
}

function wsnFindRole(el, role) {
  if (role !== 'generic') return role;
  const tag = el.tagName;
  if (tag === 'TH') return el.nextElementSibling && el.nextElementSibling.tagName === 'TD' ? 'rowheader' : 'columnheader';
  if (tag === 'DT') return 'term';
  if (tag === 'CAPTION' || tag === 'FIGCAPTION' || tag === 'LEGEND') return 'caption';
  return role;
}

// The value a header or term labels: the cell beside a row header, the
// definition after a term.
function wsnFactAnswer(el) {
  let next = el.nextElementSibling;
  if (el.tagName === 'TH') {
    if (!next || next.tagName !== 'TD') return '';
  } else if (el.tagName === 'DT') {
    while (next && next.tagName !== 'DD') next = next.nextElementSibling;
    if (!next) return '';
  } else {
    return '';
  }
  return wsnClean(next.innerText === undefined ? next.textContent : next.innerText, WSN_TEXT_LIMIT);
}
"""
