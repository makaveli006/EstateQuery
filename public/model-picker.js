/*
 * Model picker for the EstateQuery chat UI.
 *
 * Adds a chip to the message box showing the current model; clicking it opens a menu of
 * models. The choice is posted to /api/model and applies from the next message.
 *
 * Why this is custom code: Chainlit's built-in model switch ("chat profiles") restarts the
 * chat when changed, which reloads the page and discards a half-typed message. This picker
 * changes nothing in Chainlit's own state, so the conversation and the draft are kept.
 */
(() => {
  "use strict";

  // Chainlit loads only one custom script, so the sidebar layout (shell.js) is loaded from here.
  const shell = document.createElement("script");
  shell.src = "/public/shell.js?v=20261120";
  shell.defer = true;
  document.head.appendChild(shell);

  const STORAGE_KEY = "eq-model";
  const CHIP_ID = "eq-model-chip";
  const MENU_ID = "eq-model-menu";
  const CHEVRON = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const CHECK = '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const ARROW = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  let models = [];
  let currentId = null;
  let chip = null;
  let menu = null;
  let moreOpen = false;
  let openedWithKeyboard = false;

  const remembered = () => { try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; } };
  const remember = (id) => { try { localStorage.setItem(STORAGE_KEY, id); } catch (e) { /* private mode */ } };
  const byId = (id) => models.find((m) => m.id === id);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function saveChoice(id) {
    try {
      await fetch("/api/model", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: id }),
      });
    } catch (e) { /* the choice is retried on the next page load */ }
  }

  // ----------------------------------------------------------------- chip

  function renderChip() {
    if (!chip) return;
    const model = byId(currentId);
    chip.replaceChildren();
    if (!model) return;
    chip.append(el("span", "eq-chip-name", model.label), el("span", "eq-chip-via", model.note));
    chip.insertAdjacentHTML("beforeend", CHEVRON);
    chip.setAttribute("aria-label", `Model: ${model.label} (${model.note}). Change model`);
    chip.title = "Change model";
  }

  function buildChip() {
    chip = el("button", "eq-chip");
    chip.id = CHIP_ID;
    chip.type = "button";
    chip.setAttribute("aria-haspopup", "menu");
    chip.setAttribute("aria-expanded", "false");
    chip.addEventListener("click", (event) => {
      // A click event with detail 0 was produced by the keyboard (Enter or Space).
      openedWithKeyboard = event.detail === 0;
      menu ? closeMenu() : openMenu();
    });
    chip.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        openedWithKeyboard = true;
        if (!menu) openMenu();
      }
    });
    renderChip();
  }

  /** Put the chip next to the send button. React may rebuild that row, so this is re-run. */
  function attachChip() {
    if (!models.length) return;
    if (!chip) buildChip();
    const submit = document.getElementById("chat-submit");
    if (submit && submit.parentElement) {
      if (chip.parentElement !== submit.parentElement || chip.nextElementSibling !== submit) {
        chip.classList.remove("eq-chip-floating");
        submit.parentElement.insertBefore(chip, submit);
      }
      return;
    }
    // The composer's markup is not what we expect: keep the picker reachable anyway.
    const composer = document.getElementById("message-composer") || document.getElementById("chat-input");
    if (composer && !chip.isConnected) {
      chip.classList.add("eq-chip-floating");
      document.body.append(chip);
    }
  }

  // ----------------------------------------------------------------- menu

  function menuItem(model) {
    const item = el("button", "eq-item");
    item.type = "button";
    item.setAttribute("role", "menuitemradio");
    item.setAttribute("aria-checked", String(model.id === currentId));
    item.dataset.model = model.id;
    const text = el("span", "eq-item-text");
    const title = el("span", "eq-item-title", model.label);
    title.append(el("span", "eq-item-via", model.note));
    text.append(title, el("span", "eq-item-desc", model.description));
    const tick = el("span", "eq-item-tick");
    tick.innerHTML = CHECK;
    item.append(text, tick);
    item.addEventListener("click", () => choose(model.id));
    return item;
  }

  function buildMenu() {
    const panel = el("div", "eq-menu");
    panel.id = MENU_ID;
    panel.setAttribute("role", "menu");
    panel.setAttribute("aria-label", "Choose a model");

    const main = models.filter((m) => m.tier !== "more");
    const more = models.filter((m) => m.tier === "more");
    main.forEach((m) => panel.append(menuItem(m)));

    if (more.length) {
      panel.append(el("div", "eq-divider"));
      const toggle = el("button", "eq-more");
      toggle.type = "button";
      toggle.setAttribute("role", "menuitem");
      toggle.setAttribute("aria-expanded", String(moreOpen));
      toggle.append(el("span", "", "More models"));
      toggle.insertAdjacentHTML("beforeend", ARROW);
      const group = el("div", "eq-more-group");
      group.hidden = !moreOpen;
      more.forEach((m) => group.append(menuItem(m)));
      toggle.addEventListener("click", () => {
        moreOpen = !moreOpen;
        group.hidden = !moreOpen;
        toggle.setAttribute("aria-expanded", String(moreOpen));
        positionMenu();
        if (moreOpen) group.querySelector(".eq-item")?.focus();
      });
      panel.append(toggle, group);
    }

    panel.append(el("div", "eq-divider"));
    panel.append(el("p", "eq-note", "Applies from your next message. Your conversation is kept."));
    panel.addEventListener("keydown", onMenuKey);
    return panel;
  }

  function focusable() {
    return Array.from(menu.querySelectorAll("button")).filter((b) => b.offsetParent !== null);
  }

  function onMenuKey(event) {
    if (!menu) return;  // already closed by the document-level Escape handler
    const items = focusable();
    const index = items.indexOf(document.activeElement);
    if (event.key === "ArrowDown") { event.preventDefault(); items[(index + 1) % items.length]?.focus(); }
    else if (event.key === "ArrowUp") { event.preventDefault(); items[(index - 1 + items.length) % items.length]?.focus(); }
    else if (event.key === "Home") { event.preventDefault(); items[0]?.focus(); }
    else if (event.key === "End") { event.preventDefault(); items[items.length - 1]?.focus(); }
    else if (event.key === "Escape") { event.preventDefault(); closeMenu(true); }
    else if (event.key === "Tab") { closeMenu(); }
  }

  function positionMenu() {
    if (!menu || !chip) return;
    const anchor = chip.getBoundingClientRect();
    const gap = 8;
    const width = Math.min(360, window.innerWidth - 16);
    menu.style.width = `${width}px`;
    const height = menu.offsetHeight;
    // Open above the chip; if there is no room, open below it.
    let top = anchor.top - gap - height;
    if (top < 8) top = Math.min(anchor.bottom + gap, window.innerHeight - height - 8);
    let left = anchor.right - width;
    left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
    menu.style.top = `${Math.max(8, top)}px`;
    menu.style.left = `${left}px`;
  }

  function onOutside(event) {
    if (menu && !menu.contains(event.target) && !chip.contains(event.target)) closeMenu();
  }

  function onDocumentKey(event) {
    if (event.key === "Escape" && menu) closeMenu(true);
  }

  function openMenu() {
    menu = buildMenu();
    document.body.append(menu);
    chip.setAttribute("aria-expanded", "true");
    positionMenu();
    const selected = menu.querySelector('.eq-item[aria-checked="true"]');
    if (openedWithKeyboard) (selected && selected.offsetParent ? selected : focusable()[0])?.focus();
    document.addEventListener("mousedown", onOutside, true);
    document.addEventListener("keydown", onDocumentKey, true);
    window.addEventListener("resize", positionMenu);
  }

  function closeMenu(returnFocus) {
    if (!menu) return;
    menu.remove();
    menu = null;
    chip.setAttribute("aria-expanded", "false");
    document.removeEventListener("mousedown", onOutside, true);
    document.removeEventListener("keydown", onDocumentKey, true);
    window.removeEventListener("resize", positionMenu);
    if (returnFocus || openedWithKeyboard) chip.focus();
    else document.getElementById("chat-input")?.focus();   // mouse users carry on typing
  }

  function choose(id) {
    if (!byId(id)) return;
    currentId = id;
    remember(id);
    renderChip();
    closeMenu();
    saveChoice(id);
  }

  // ----------------------------------------------------------------- recovery

  /*
   * Safety net for a page loaded directly on a conversation URL (/thread/<id>).
   * In testing, such a load occasionally left the chat area empty: the sidebar appeared but
   * the conversation was never resumed. The cause was not found, so rather than leave a
   * visitor stuck, a page that still has no message box after a generous wait reloads
   * itself, at most twice, and then falls back to the start page.
   */
  const RETRY_KEY = "eq-thread-retry";
  const BLANK_AFTER_MS = 15000;

  function watchForBlankConversation() {
    const read = () => { try { return Number(sessionStorage.getItem(RETRY_KEY) || 0); } catch (e) { return 0; } };
    const write = (n) => { try { n ? sessionStorage.setItem(RETRY_KEY, String(n)) : sessionStorage.removeItem(RETRY_KEY); } catch (e) { /* ignore */ } };
    if (!location.pathname.startsWith("/thread/")) { write(0); return; }
    setTimeout(() => {
      if (!location.pathname.startsWith("/thread/")) return;   // the visitor moved on
      if (document.getElementById("chat-input")) { write(0); return; }
      const tries = read();
      if (tries >= 2) { write(0); location.replace("/"); return; }
      write(tries + 1);
      location.reload();
    }, BLANK_AFTER_MS);
  }

  // ----------------------------------------------------------------- start

  async function start() {
    watchForBlankConversation();
    try {
      const response = await fetch("/api/models", { credentials: "same-origin" });
      const data = await response.json();
      models = data.models || [];
      if (!models.length) return;
      const saved = remembered();
      if (byId(saved)) {
        currentId = saved;
        saveChoice(saved);            // the server forgets choices when it restarts
      } else {
        let serverChoice = null;
        try {
          serverChoice = (await (await fetch("/api/model", { credentials: "same-origin" })).json()).model;
        } catch (e) { /* fall through to the default */ }
        currentId = byId(serverChoice) ? serverChoice : (byId(data.default) ? data.default : models[0].id);
      }
    } catch (e) {
      return;                         // without the model list there is nothing to pick from
    }

    attachChip();
    let queued = false;
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; attachChip(); });
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
