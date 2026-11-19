/*
 * ChatGPT-style sidebar for the EstateQuery chat UI (loaded by model-picker.js).
 *
 * Adds, inside Chainlit's own sidebar:
 *   - a "New chat" row (it presses Chainlit's own new-chat button, so behaviour is unchanged),
 *   - Scheduled, Library, Plugins and Explore rows marked "Soon",
 *   - a Projects heading with one showcase project,
 *   - a "Recents" heading above the conversation list,
 *   - a profile block ("DarGlobal", plan "Plus") at the bottom.
 *
 * Everything except "New chat" is a placeholder for the demo and does nothing. The layout of the
 * header and the welcome screen is in shell.css.
 *
 * Chainlit renders the sidebar with React and may re-render it, so all injected nodes are kept in
 * one container that is put back when it disappears. The script never touches the conversation,
 * makes no requests, and does nothing at all if the sidebar markup is not there.
 */
(() => {
  "use strict";

  const SHELL_ID = "eq-shell";
  const PROFILE_ID = "eq-profile";
  const PROJECT_NAME = "Branded Residences";
  const PROFILE_NAME = "DarGlobal";
  const PROFILE_PLAN = "Plus";

  const svg = (paths) =>
    '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round">' + paths + "</svg>";

  const ICONS = {
    compose: svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>'),
    scheduled: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    library: svg('<path d="M4 4v16"/><path d="M9 4v16"/><path d="M14.2 5.2l4.5 14.6"/><path d="M14.2 5.2l3.6-1.1"/>'),
    plugins: svg('<circle cx="12" cy="12" r="3.2"/><path d="M15.2 12v1.3a2.3 2.3 0 0 0 4.6 0V12a7.8 7.8 0 1 0-3.1 6.2"/>'),
    explore: svg('<rect x="4" y="4" width="6" height="6" rx="1.6"/><rect x="14" y="4" width="6" height="6" rx="1.6"/>' +
                 '<rect x="4" y="14" width="6" height="6" rx="1.6"/><rect x="14" y="14" width="6" height="6" rx="1.6"/>'),
    folder: svg('<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.2h7.5A2.5 2.5 0 0 1 21 9.7v7.8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5Z"/>'),
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function row(tag, icon, label) {
    const node = el(tag, "eq-row");
    node.insertAdjacentHTML("beforeend", ICONS[icon]);
    node.append(el("span", "eq-row-label", label));
    return node;
  }

  /** A row that is shown but does nothing. */
  function placeholderRow(icon, label, withPill) {
    const node = row("div", icon, label);
    node.setAttribute("role", "button");
    node.setAttribute("aria-disabled", "true");
    node.setAttribute("tabindex", "-1");
    node.title = "Coming soon";
    if (withPill) node.append(el("span", "eq-soon", "Soon"));
    return node;
  }

  function newChatRow() {
    const node = row("button", "compose", "New chat");
    node.type = "button";
    node.id = "eq-newchat";
    node.classList.add("eq-row-primary");
    node.addEventListener("click", () => {
      // Chainlit's own button holds the new-chat logic (clear the chat, go to the start page).
      const original = document.getElementById("new-chat-button");
      if (original) original.click();
    });
    return node;
  }

  function buildShell() {
    const shell = el("nav", "");
    shell.id = SHELL_ID;
    shell.setAttribute("aria-label", "Navigation");
    shell.append(
      newChatRow(),
      placeholderRow("scheduled", "Scheduled", true),
      placeholderRow("library", "Library", true),
      placeholderRow("plugins", "Plugins", true),
      placeholderRow("explore", "Explore", true),
      el("div", "eq-heading", "Projects"),
      placeholderRow("folder", PROJECT_NAME, false),
      el("div", "eq-heading eq-heading-recents", "Recents"),
    );
    return shell;
  }

  function buildProfile() {
    const profile = el("div", "");
    profile.id = PROFILE_ID;
    const text = el("div", "eq-profile-text");
    text.append(el("span", "eq-profile-name", PROFILE_NAME), el("span", "eq-profile-plan", PROFILE_PLAN));
    profile.append(el("span", "eq-avatar", "DG"), text);
    return profile;
  }

  /** Put the injected nodes in place; safe to call as often as needed. */
  function attach() {
    const content = document.querySelector('[data-sidebar="content"]');
    if (!content) return;                                   // no sidebar here: change nothing

    let shell = document.getElementById(SHELL_ID);
    if (!shell || shell.parentElement !== content || content.firstElementChild !== shell) {
      if (shell) shell.remove();
      shell = buildShell();
      content.prepend(shell);
    }

    let profile = document.getElementById(PROFILE_ID);
    if (!profile || profile.previousElementSibling !== content) {
      if (profile) profile.remove();
      profile = buildProfile();
      content.after(profile);
    }
  }

  function start() {
    attach();
    let queued = false;
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; attach(); });
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
