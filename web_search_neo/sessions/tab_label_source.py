"""Page-side source of the tab-strip label (pure JavaScript, no runtime imports)."""

# A tab-strip label so a human looking at the tab bar can tell which tab each
# agent is driving. `agent_label` already names the owner to *readers* of
# browser_status; this makes it visible where the user is actually looking.
#
# The label lives in the real `<title>` element - that is what the tab strip
# shows - while page-side JavaScript keeps reading and writing the *unlabelled*
# title through a shadow accessor on the document instance, so a page that
# compares or derives from `document.title` never sees the prefix. Writes
# through the shadow go out prefixed; direct rewrites of the `<title>` element
# are caught by a MutationObserver and re-prefixed; statically parsed titles
# are picked up again on DOMContentLoaded. The script is installed with
# Page.addScriptToEvaluateOnNewDocument, so it runs before the page's own
# scripts in every new document and survives navigations and reloads.
_TAB_LABEL_SCRIPT_TEMPLATE = r"""
(() => {
  const PREFIX = __WSN_TAB_LABEL_PREFIX__;
  const KEY = "__wsnTabLabel";
  const previous = window[KEY];
  if (previous && previous.prefix === PREFIX) return;
  if (previous && typeof previous.restore === "function") {
    try { previous.restore(); } catch (error) {}
  }
  const protoDesc = Object.getOwnPropertyDescriptor(Document.prototype, "title");
  const state = { prefix: PREFIX, real: "", applying: false, observer: null };
  window[KEY] = state;
  function titleElement() {
    return document.querySelector ? document.querySelector("title") : null;
  }
  function shownTitle() {
    const el = titleElement();
    return el && typeof el.textContent === "string" ? el.textContent : "";
  }
  function writeTitle(wanted) {
    if (protoDesc && typeof protoDesc.set === "function") {
      protoDesc.set.call(document, wanted);
      return;
    }
    let el = titleElement();
    if (!el && document.head && document.createElement) {
      el = document.createElement("title");
      document.head.appendChild(el);
    }
    if (el) el.textContent = wanted;
  }
  function render() {
    const wanted = state.prefix + state.real;
    if (shownTitle() === wanted) return;
    state.applying = true;
    try {
      writeTitle(wanted);
    } finally {
      state.applying = false;
    }
  }
  function adoptShown() {
    const shown = shownTitle();
    if (shown.indexOf(state.prefix) === 0) {
      state.real = shown.slice(state.prefix.length);
    } else {
      state.real = shown;
    }
  }
  function sync() {
    if (state.applying) return;
    if (shownTitle() === state.prefix + state.real) return;
    adoptShown();
    render();
  }
  try {
    Object.defineProperty(document, "title", {
      configurable: true,
      enumerable: true,
      get() { return state.real; },
      set(value) {
        const text = value === null || value === undefined ? "" : String(value);
        state.real = text.indexOf(state.prefix) === 0
          ? text.slice(state.prefix.length)
          : text;
        render();
      },
    });
  } catch (error) {
    // A page that froze its own document.title keeps working unlabelled to
    // its scripts; the observer below still prefixes what the tab strip shows.
  }
  try {
    state.observer = new MutationObserver(sync);
    state.observer.observe(document, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  } catch (error) {}
  if (document.addEventListener) {
    document.addEventListener("DOMContentLoaded", sync);
  }
  adoptShown();
  render();
  state.restore = function () {
    try {
      if (state.observer) state.observer.disconnect();
    } catch (error) {}
    try {
      delete document.title;
    } catch (error) {}
    try {
      if (Object.getOwnPropertyDescriptor(document, "title")) {
        const el = titleElement();
        if (el) el.textContent = state.real;
      } else {
        document.title = state.real;
      }
    } catch (error) {}
    try {
      delete window[KEY];
    } catch (error) {}
  };
})();
"""

_TAB_LABEL_RESTORE_SCRIPT = (
    "(() => { const labelled = window.__wsnTabLabel;"
    " if (labelled && typeof labelled.restore === 'function') labelled.restore();"
    " return true; })()"
)
