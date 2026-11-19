// ==UserScript==
// @name         EstateQuery Wasalt collector
// @namespace    https://github.com/makaveli006/EstateQuery
// @version      1.0.0
// @description  Saves the listing data of the Wasalt pages YOU open, for the EstateQuery assignment. Read-only: it only looks at the page you are viewing.
// @match        https://wasalt.sa/en/property/*
// @run-at       document-idle
// @grant        GM_getValue
// @grant        GM_setValue
// ==/UserScript==

/*
 * What this does
 *   When you open a Wasalt listing page in your own browser, it copies the listing's property
 *   data (price, type, location, bedrooms, area, description) out of the page you are already
 *   looking at, and keeps it in this script's storage. A small badge in the corner shows how
 *   many listings are saved. "Download" saves them as wasalt-collected.json.
 *
 * What it does NOT do
 *   - It never loads a page, makes a request, clicks, or navigates. You open every page yourself,
 *     and you pass any "verify you are human" check yourself.
 *   - It never saves owner or agent names, phone numbers, or licence numbers: only an allowlist
 *     of top-level fields is kept, and everything else on the page is ignored.
 *
 * Setup (Chrome)
 *   1. Install the Tampermonkey extension.
 *   2. In chrome://extensions, open Tampermonkey's Details and switch on "Allow user scripts"
 *      (newer Chrome versions need this).
 *   3. Tampermonkey > Create a new script, replace its content with this file, and save.
 *   4. Open the listing links from wasalt_to_open.html one by one.
 *   5. When the badge shows enough listings, click Download and keep the file.
 */
(function () {
  "use strict";

  const STORE = "eq_wasalt_collected";
  // Only these top-level fields of the page's listing data are ever kept.
  const KEEP = ["id", "location", "attributes", "additionalAttributes", "propertyInfo", "publishedAt", "updatedAt"];
  const WAIT_MS = Number(window.__EQ_WAIT_MS) || 40000;   // time allowed for a person to pass a check
  const POLL_MS = 500;
  const BADGE_ID = "eq-collector";

  const load = () => {
    try { return JSON.parse(GM_getValue(STORE, "{}")) || {}; } catch (e) { return {}; }
  };
  const save = (store) => GM_setValue(STORE, JSON.stringify(store));

  /** The listing data embedded in the page being viewed, or null if this page has none. */
  function readListing() {
    const el = document.getElementById("__NEXT_DATA__");
    if (!el) return null;
    try {
      const details = JSON.parse(el.textContent).props.pageProps.propertyDetailsV3;
      return details && details.id != null ? details : null;
    } catch (e) {
      return null;
    }
  }

  function pickAllowed(details) {
    const kept = {};
    for (const key of KEEP) if (key in details) kept[key] = details[key];
    return kept;
  }

  // ------------------------------------------------------------------ badge

  function button(label, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.style.cssText = "margin-left:8px;padding:2px 8px;border:1px solid #888;border-radius:6px;background:#fff;color:#111;cursor:pointer;font:inherit";
    b.addEventListener("click", onClick);
    return b;
  }

  function showBadge() {
    let badge = document.getElementById(BADGE_ID);
    if (!badge) {
      badge = document.createElement("div");
      badge.id = BADGE_ID;
      badge.style.cssText = "position:fixed;left:12px;bottom:12px;z-index:2147483647;padding:8px 10px;border-radius:10px;" +
        "background:#1f6f5c;color:#fff;font:13px/1.2 system-ui,sans-serif;box-shadow:0 2px 10px rgba(0,0,0,.35)";
      const text = document.createElement("span");
      text.id = BADGE_ID + "-count";
      badge.append(text,
        button("Download", download),
        button("Clear", () => { if (window.confirm("Forget all saved listings?")) { save({}); showBadge(); } }));
      document.body.append(badge);
    }
    const count = Object.keys(load()).length;
    badge.querySelector("#" + BADGE_ID + "-count").textContent = "EstateQuery: " + count + " saved";
  }

  function download() {
    const entries = Object.values(load());
    const blob = new Blob([JSON.stringify(entries)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "wasalt-collected.json";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  }

  // ------------------------------------------------------------------ collect

  function collect() {
    const details = readListing();
    if (!details) return false;
    const store = load();
    const id = String(details.id);
    store[id] = {
      id: id,
      url: location.origin + location.pathname,
      collected_at: new Date().toISOString(),
      details: pickAllowed(details),
    };
    save(store);
    showBadge();
    return true;
  }

  // Wait for the listing data. A person may first have to pass a check; the page then reloads
  // or swaps in the real content, so keep looking for a while and give up quietly.
  const started = Date.now();
  (function poll() {
    if (collect()) return;
    if (Date.now() - started < WAIT_MS) setTimeout(poll, POLL_MS);
  })();
})();
