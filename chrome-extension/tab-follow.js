// Chrome can replace the tab under a page (prerender/instant activation, a
// discarded tab coming back): chrome.tabs.onReplaced reports the swap and the
// old id stops existing. An MCP session still holds the old id, so without a
// record every later command failed with "No tab with given id" while the page
// it was driving sat right there. The follower keeps exactly that record - and
// nothing looser: a tab the lost one opened, or one on the same URL, is not a
// replacement and is never followed, because this is the user's own browser.
// Commands addressed to a replaced tab reach its successor, the answer says so
// (tab_followed), and the server can ask `tabs.resolve` who took over.
const REPLACED_KEY = "wsn_replaced_tabs";
const LIMIT = 200;

export function createTabFollower(tabsApi, sessionStorage) {
  const replaced = new Map();   // removed id -> added id
  let restoring = null;

  // One shared promise: every caller waits for the same restore instead of the
  // second one racing ahead of a restore the first one started.
  function restore() {
    if (!restoring) {
      restoring = (async () => {
        try {
          const stored = (await sessionStorage?.get(REPLACED_KEY))?.[REPLACED_KEY];
          for (const [from, to] of Object.entries(stored || {})) {
            if (!replaced.has(Number(from))) replaced.set(Number(from), Number(to));
          }
        } catch (error) {
          console.warn("bridge: could not restore tab replacements", error);
        }
      })();
    }
    return restoring;
  }

  function persist() {
    const entries = [...replaced.entries()].slice(-LIMIT);
    replaced.clear();
    for (const [from, to] of entries) replaced.set(from, to);
    Promise.resolve(sessionStorage?.set({[REPLACED_KEY]: Object.fromEntries(entries)}))
      .catch(() => {});
  }

  function follow(tabId) {
    let current = Number(tabId);
    for (let hops = 0; hops < 16 && replaced.has(current); hops += 1) current = replaced.get(current);
    return current;
  }

  async function alive(tabId) {
    try {
      return Boolean(await tabsApi.get(Number(tabId)));
    } catch (error) {
      return false;
    }
  }

  tabsApi?.onReplaced?.addListener((addedTabId, removedTabId) => {
    replaced.set(Number(removedTabId), Number(addedTabId));
    persist();
  });

  return {
    // Rewrites a command's tabId to the tab that replaced it; returns the
    // params unchanged (same object) when there is no replacement on record.
    async redirect(params) {
      if (!params || params.tabId === undefined || params.tabId === null) return params;
      await restore();
      const followed = follow(params.tabId);
      return followed === Number(params.tabId) ? params : {...params, tabId: followed};
    },
    async resolve(tabId) {
      await restore();
      const requested = Number(tabId);
      const successor = follow(requested);
      const replacedBy = successor === requested ? null : successor;
      return {
        tabId: requested,
        alive: await alive(requested),
        replaced_by: replacedBy,
        successor_alive: replacedBy === null ? null : await alive(replacedBy),
      };
    },
  };
}
