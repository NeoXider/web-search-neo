// The manifest version is a promise about the folder; this hash is a promise
// about what Chrome actually executed. An unpacked extension keeps running the
// worker it loaded until someone presses Reload, so a folder that gained new
// commands can sit behind an old worker whose version string still matches -
// and only comparing code, not versions, sees it. Every module the worker
// imports counts: a changed helper is changed code too. The Python side
// (chrome_bootstrap.CODE_FILES) hashes the same files in the same order;
// tests/test_field_report_followup.py keeps the two lists and the import graph
// in step. bridge-token.js is the machine secret, not code, and never counts.
export const CODE_FILES = [
  "service-worker.js",
  "events.js",
  "agent-badges.js",
  "agent-activity.js",
  "background-capture.js",
  "tab-follow.js",
  "bridge-auth.js",
  "code-hash.js",
];

export async function selfCodeHash(fetchImpl, getURL, hexOf) {
  try {
    const parts = [];
    for (const name of CODE_FILES) {
      const response = await fetchImpl(getURL(name));
      parts.push(new Uint8Array(await response.arrayBuffer()));
    }
    const joined = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
    let offset = 0;
    for (const part of parts) {
      joined.set(part, offset);
      offset += part.length;
    }
    return hexOf(new Uint8Array(await crypto.subtle.digest("SHA-256", joined)));
  } catch (error) {
    console.warn("bridge: could not hash the companion code", error);
    return null;
  }
}
