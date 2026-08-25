# Changelog

## 1.8.2

Бенчмарк, гонявший браузерные задачи, за час забил рабочий Chrome пользователя
группами вкладок. Причина оказалась в умолчании: `profile_mode` по умолчанию —
`current`, то есть агент, ни разу не упомянувший режим, открывает страницы
именно в том браузере, где человек работает.

- Переменная окружения **`WSN_FORBID_CURRENT_PROFILE`**: при ней любой запрос в
  рабочий профиль понижается до `temporary` — своего одноразового Chrome.
  Именно понижение, а не отказ: работа должна продолжиться, просто в другом
  месте. Подмена не тихая, фактический режим возвращается в каждом ответе как
  `profile_mode`.
- Покрыты все написания: `auto` больше не скатывается в рабочий профиль, а
  `extension` — это второе имя того же `current`, и через него не проскочить.
- Почему на этой стороне, а не в промпте: попросить агента можно, но слабая
  модель просьбу проигнорирует, а расплачивается за это человек, который просто
  запустил задачу.

## 1.8.1

Агента попросили прибрать вкладки в браузере, и выяснилось, что закрыть он умеет ровно одну —
ту, которую сам открыл. `close` на привязанной вкладке отцепляется и оставляет её открытой
(что правильно для одолженной вкладки и бесполезно для ненужной), а `window.close()` из
страницы Chrome игнорирует. Расширение при этом всегда умело `tabs.remove` по любому id —
дыра была только на стороне Python.

- Действие **`close_tabs`**: закрывает названные вкладки пользовательского Chrome по id из
  `web_info(topic='browser_tabs')`. Переключателя «закрыть всё» нет намеренно — закрытие
  необратимо, поэтому каждая вкладка называется явно.
- Две категории отказов вместо закрытия, потому что именно так это ломается неприятнее всего:
  **закреплённые** вкладки — это набор, который человек держит осознанно, и он последнее, что
  должна унести зачистка «лишнего»; вкладки, которые **ведёт другой агент**, — закрыть такую
  значит выдернуть страницу из-под чужой сессии на ходу. Снимаются поимённо через
  `include_pinned` / `include_claimed`, и каждый отказ говорит, в какое правило упёрся.
- Исход считается по **свежему списку вкладок**, а не по ответу расширения: `tabs.remove`
  сообщает о неудаче всякий раз, когда `chrome.tabs.remove` бросает исключение, а типичная
  причина этого — вкладка, которую пользователь только что закрыл руками. Вкладка, которой уже
  нет, попадает в `skipped` с `already_gone`, потому что это и есть запрошенный результат.
- Сессия, сидевшая на закрытой вкладке, **забывается**, а её claim освобождается. Иначе она
  продолжала бы отзываться на своё имя, и следующее действие ушло бы на id, который Chrome
  успел переиспользовать, — с ошибкой совсем в другом месте.
- У `close` появился явный параметр **`close_tab`**: способность закрыть привязанную вкладку в
  `browser_tools` была всегда, но наружу через действие не выводилась.

## 1.8.0

Двое суток живой работы — около 130 откликов на вакансии через параллельных агентов — дали
три дефекта, и все три об одном: сервер отвечал уверенно и неправильно. Молчал там, где
страница уже стояла; ругался там, где всё получилось. Каждый стоил потерянной работы.

- Научить детект видеть **невидимую капчу**. Раньше обход виджетов выбрасывал всё, у чего нет
  видимой коробки, — а невидимый Turnstile именно такой: в DOM есть `div.cf-turnstile` и
  `iframe` с `challenges.cloudflare.com`, но ни картинки, ни чекбокса. Обработчик отправки на
  `apply.workable.com` ждёт токен, которого никто не выдаст: кнопка навсегда уходит в
  «Submitting…», POST не уходит, консоль чистая. За один заход так потеряно двенадцать
  подходящих вакансий, и повтор со `stealth` и свежей сессией давал ровно то же самое.
  Теперь каждая сводка страницы несёт `invisible_challenge_pending`, а при `true` — ещё и
  `invisible_challenge` с вендором, состоянием (`token_empty` — скрытое поле
  `cf-turnstile-response` / `g-recaptcha-response` / `h-captcha-response` / `smart-token`
  пустое; `widget_hidden` — виджет отрисован, поля ещё нет), доказательствами и подсказкой.
  Это блокирует форму, а не страницу, поэтому `challenge_detected` остаётся `false`: агент не
  должен парковаться на три минуты там, где страница читается.
- Признать, что заполненный токен закрывает вопрос: скрытый контейнер, который вендор
  оставляет в DOM после решения, сам по себе больше ни о чём не говорит.
- `captcha` с `op=detect` больше не отвечает `captcha_present=false` на такой странице, а
  ожидание (`mode='wait'`) больше не считает невидимую капчу решённой: пустое токен-поле —
  это то же самое ожидание, и «resolved» поверх него означало «форму можно отправлять», когда
  было нельзя.
- Назвать **зависшую отправку** вслух. Если `click` или `click_text` не породил ни одного
  сетевого запроса, а на странице нерешённый невидимый виджет, в ответе появляются
  `submit_blocked_by_challenge=true` и `submit_block_reason`. Оба факта у сервера уже были —
  перехват сети и обход DOM, — просто никто их не сводил вместе. Проверка идёт по общему
  сливу сетевого журнала (`_drain_network_rows`), одинаковому для Selenium и для компаньона,
  и учитывает запросы в полёте: «POST ещё не закончился» — не то же самое, что «POST не было».
- Сохранить **переводы строк в contenteditable**. `Input.insertText` вставляет один текстовый
  узел, а в нём `\n` рисуется пробелом, поэтому многострочное письмо приезжало в тело Gmail
  одним абзацем — и `fill` честно возвращал «The control did not take the value», сравнив
  ожидаемое с фактическим. Теперь текст набирается построчно, между строками — мягкий перенос
  (Shift+Enter, а не Enter: в чат-композере Enter отправляет недописанное). Чтение обратно
  идёт через `innerText`, а не `textContent`, который склеивал абзацы редактора в одну строку
  и превращал удавшуюся запись в «отказ».
- Если редактор всё равно схлопнул переносы, сказать это прямо: ошибка теперь называет
  причину и рабочий путь — `run_script` с `user_gesture=true` и `navigator.clipboard.writeText`,
  затем настоящий `Ctrl+V` через `input`. Ограничение Telegram Web (`#editable-message-text`,
  Teact: запись в DOM не поднимает состояние компонента, кнопка отправки остаётся микрофоном)
  записано в контракт `fill` там же.
- Перестать считать пустой `input[type=file]` доказательством провала **загрузки**. Teamtailor
  и любой Dropzone забирают файл с инпута и очищают его — резюме уже на S3, чип с именем на
  экране, а `upload` возвращал `success:false`. Теперь ответ несёт `upload_state`: `attached`
  (инпут держит файлы — точный случай), `taken_by_widget` (инпут пуст, но имя файла появилось
  на странице или ушёл POST/PUT/PATCH после присоединения) или `unconfirmed` (сказать нельзя).
  `unconfirmed` — не отказ: `note` описывает, как проверить (искать имя через `page_text` и
  `elements`, запрос — через топик `network`), а `success` теперь ложь только тогда, когда
  само присоединение упало. У `fill` с `files` то же самое в `upload_states`/`upload_notes`.
- Дописать контракт там, где поменялось поведение: заметки `fill`, `upload`, `click`,
  `page_elements`, правила и раздел troubleshooting в скилле, общий список pitfalls. Бюджеты
  `capabilities` (14000) и `skill` (7000) не подняты — 13401 и 6401.

## 1.7.0

A macro is a JSON file, and now that is all it is. The write half of the `macro` action -
the recorder and the pack transport - is gone, and a checker that runs before the page does
has taken its place.

- Remove `op=record`, `op=save` and `op=cancel`, and with them the whole recording machinery:
  the per-session recording registry, the batch lock that serialised recorded dispatches, the
  interception in the action loop, and the attribution rules for a step that named no session.
  The recorder was never self-sufficient - its own contract told the caller to save the
  recording and then hand-edit the JSON to turn the changing parts into `{{placeholders}}`, so
  the path ended in an editor either way. Over a full day of live use all four working macros
  (`gmail-send`, `proton-send`, `tg-send`, `hh-reply`) were written directly as JSON and the
  recorder was not used once, while it carried a class of defects of its own: races between
  concurrent batches, steps landing in the wrong open recording, a name borrowed by an
  explicit save, steps lost when `record` was called twice.
- Remove `op=delete`, `op=export` and `op=import`. When a macro is a file, deleting one is
  deleting a file and moving a set is copying a directory; a second, weaker API for the same
  thing was one more place for a store to be chosen wrongly. The pack format goes with them.
- Add `op=validate`, which reads a macro file and dispatches nothing. Errors: an action name
  the server does not have, a required parameter missing, a parameter that is not part of that
  action and would be refused at dispatch, a placeholder used but not declared in `variables`,
  and a `{{placeholder}}` inside a `run_script` script. That last one is why this exists - the
  value is pasted into the JavaScript as raw text, so any newline, quote or backslash produces
  a broken program and the step fails with an opaque `Uncaught` from inside the page, several
  steps into a live form. Warnings, which never make a macro invalid: a declared variable no
  step uses, steps drifting between two `session_id` values, and a macro whose last meaningful
  step neither waits nor reads anything back. Every finding carries the step index, what is
  wrong, and how to fix it.
- Rewrite the `macro` recipe, the `macros` skill section and the action's own notes around the
  path that is now the real one: write the JSON, `validate`, `preview` with variables, `run`.
  The old recipe described the recorder and was simply wrong after this change.
- The macro file format is untouched. All sixteen macros in use - fourteen in a project store,
  two in the per-user one - load, resolve and preview exactly as before.


## 1.6.0

A day of real use - about a hundred job applications filed by five agents through one server -
produced four defects, all of them about several agents sharing one MCP server.

- Raise the default session cap from 4 to 8. Four was chosen when a session meant a Chrome
  process; in `profile_mode="current"`, which is what agents actually use, a session is one tab
  of a Chrome that is already running and costs tens of megabytes, not hundreds. In the run
  above four agents took every slot and the fifth could not open a single page, so it filed
  nothing at all - a far worse outcome than the memory the low number was protecting. Eight
  covers an ordinary fan-out with room to spare and still stops a leaking model early. The
  ceiling stays 64: it exists to catch a typo, and a desktop runs out of memory long before it.
- Make the cap settable from the companion extension's popup, under Settings next to the bridge
  port. The number rides in the hello the extension already sends, which the daemon already
  relays to every connected MCP server, so no new channel was needed. `WEB_SEARCH_NEO_MAX_SESSIONS`
  in the server's own environment still wins - a number deployed there was said about that
  server - and the popup hint says so. `browser_status` and `capabilities` report which of the
  three sources the cap in force came from.
- Give `close_all` an owner. It used to close every session in the process, which inside one
  server is every *agent's* session: one subagent tidying up ended four others' work mid-form,
  and the only defence was a line in every brief telling agents never to call it. It now
  defaults to `scope="mine"` and closes the sessions carrying the caller's `agent_label` (with
  no label, the unlabelled ones), always reporting `kept_sessions` and who owns them.
  `scope="all"` (or `include_foreign=true`) is the old behaviour, kept and explicit. The
  shutdown hook still closes everything, because at process exit nobody is left to own a tab.
- Bound perception answers by size, not only by count. `page_elements` had `limit` and `offset`
  but nothing measuring the answer: 200 controls on a live job board came back as 83,616
  characters, which the model that asked could not receive at all. `page_elements`, `page_outline`
  and `find` now take `max_chars` (default 18,000), trim to a prefix, restate `returned` and
  `range[*].next_offset` so the continuation offset points at the first entry that was not sent,
  and say what was dropped in `budget_note`. The budget is shared round-robin across the
  categories, so a page whose buttons matter is not handed an answer made entirely of links.
  `page_text` and `element_text` already had honest budgets and are unchanged.
- Let a session say who opened it. `open` and `attach_tab` take an optional `agent_label`;
  omitting it is not an error. `browser_status` now carries the whole roster - per session the
  owner label, tab id and group, profile mode, the page it was last seen on, when it was created
  and last used, idle seconds, and whether another thread is inside it - plus `N of M` occupancy
  and where the cap came from. It is answered entirely from memory: asking each tab for its URL
  would mean waiting on the lock its own agent holds, and status is what a stuck run reads first.
  `capabilities` reports the same occupancy under `limits`, because "8" tells a blocked agent
  nothing that failing would not have told it, while "0 free" does.

## 1.5.0

- Key macro recordings by `session_id`. A single shared recording collected every dispatched
  action, so with two agents in one server an agent recording a task captured the other's
  actions and replayed them. Recordings are now per session and independent.
- Infer the recording for `save` and `cancel` when one is open, and refuse to guess when
  several are, naming the sessions. An action with no session of its own joins the only open
  recording, and when several are open it is reported as `unattributed_steps` instead of
  attributed by luck.
- Attribute an action to the session its schema defaults to, rather than treating an unset
  `session_id` as no session at all.
- Serialise only the batches that touch a recorded session; everything else stays concurrent.
- Accept `session_id` on `macro op=run` and `op=preview` to point a recorded macro at another
  tab, refusing to collapse a macro that already drives two sessions.
- Refuse any DevTools method outside an explicit allowlist inside the companion, so an
  authenticated local peer holds the contract's capabilities rather than the whole protocol.
  A test compares the allowlist against the server's call sites so they cannot drift apart.
- Make the companion's bridge port a stored setting in the popup, replacing the documented
  edit of `BRIDGE_URL` in an installed extension. It validates the range, reconnects at once,
  and survives a browser restart.
- Show the next reconnect attempt in the popup, so a deliberate backoff no longer reads as a
  broken bridge.
- Keep the Windows daemon-spawn branch importable on other platforms, and wait for the
  compositor before reading a container's scroll position, fixing both Linux CI failures.
- Check that the companion manifest version matches the server's, and that popup.js, popup.html
  and popup.css describe the same page.

## 1.4.1

- Declare a macro's placeholders from its steps on every read, not only when `save` wrote the
  file, so a hand-written macro reports what it wants through `op=list` and `op=show` instead
  of appearing to want nothing until a run fails.
- Check every resolved step against its published action schema during `op=preview`, reporting
  `steps_valid` and a `problems` list, so a mistyped parameter in a hand-edited file is found
  before any step dispatches rather than midway through a replay.

## 1.4.0

- Resolve `project_root: "auto"` for every macro operation: `WEB_SEARCH_NEO_PROJECT_ROOT`, then
  the nearest ancestor with `.web-search-neo`, then the nearest repository root.
- Let `WEB_SEARCH_NEO_PROJECT_ROOT` supply the default project for calls that pass no
  `project_root`, so an MCP client can be configured once per project.
- Report `scope`, `project_root`, `storage`, and `other_store` on every macro answer.
- Accept a bare step list as a macro file, and take the macro's identity from its file name.
- Add `macro op=export` and `op=import` for whole macro sets, with all-or-nothing validation and
  a refusal to overwrite an existing name unless asked.
- Write a `README.md` into every macro store describing the file format.
- Stop listing the guarded-operation ledger as a macro, and report a file that cannot be read
  as a macro as broken instead of summarising it as empty.
- Fix a recording started with a project that resolves to none: it no longer derives a project
  directory from the per-user store's path.
- Add `web_info(topic="actions")`: the action index alone, narrowable with `params.group`.
- Add detailed skill sections behind `web_info(topic="skill", params={"section": "<name>"})`
  covering start, loop, locators, forms, macros, guarded, parallel, search, diagnostics, games,
  and troubleshooting.

## 1.3.11

- Make `guard.resource_sha256` mandatory for `guarded_stage`.
- Compute SHA-256 from the current `guard.resource_path` bytes and fail closed on a missing,
  malformed, or mismatched digest.
- Normalize a verified digest to lowercase, return it from guarded stage/commit, and persist it
  in the project-local one-time checkpoint ledger.
- Document that guarded commit attempts its terminal action once and that confirmation proof
  must be collected separately without automatically retrying that action.
