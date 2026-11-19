"""The ChatGPT-style sidebar (public/shell.js and shell.css), run in headless Chromium.

A static page reproduces the markup of Chainlit's sidebar (the `data-sidebar` attributes and ids the
real app uses). Playwright serves it together with the real script and stylesheet and aborts every
other request. Skipped when Playwright or Chromium is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

PUBLIC = Path(__file__).resolve().parent.parent / "public"
ORIGIN = "https://harness.test"

PAGE = """<!doctype html>
<html class="dark"><head><meta charset="utf-8"><link rel="stylesheet" href="/public/shell.css"></head>
<body>
  <div data-sidebar="sidebar">
    <div data-sidebar="header"><div class="flex items-center justify-between">
      <button id="sidebar-trigger-button" data-sidebar="trigger">toggle</button>
      <div><button id="search-button">search</button><div><button id="new-chat-button">new</button></div></div>
    </div></div>
    <div data-sidebar="content">
      <div data-sidebar="group"><ul data-sidebar="menu"><div id="thread-history">
        <div data-sidebar="group"><div data-sidebar="group-label">Today</div>
          <ul><li id="thread-1">First chat</li><li id="thread-2">Second chat</li></ul></div>
        <div data-sidebar="group"><div data-sidebar="group-label">Yesterday</div>
          <ul><li id="thread-3">Older chat</li></ul></div>
      </div></ul></div>
    </div>
  </div>
  <button id="user-nav-button">G</button>
  <div id="welcome-screen"><img class="logo" alt="logo" src="/public/logo.png"><div>composer</div></div>
  <script>
    window.newChatClicks = 0;
    document.getElementById('new-chat-button').addEventListener('click', () => { window.newChatClicks += 1; });
  </script>
  <script src="/public/shell.js"></script>
</body></html>"""

NO_SIDEBAR = """<!doctype html><html><head><meta charset="utf-8"></head>
<body><div id="welcome-screen">no sidebar here</div><script src="/public/shell.js"></script></body></html>"""


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception:  # noqa: BLE001 - Chromium not installed on this machine
            pytest.skip("Chromium is not installed")
        yield instance
        instance.close()


def make_page(browser, html: str = PAGE):
    ctx = browser.new_context(viewport={"width": 1100, "height": 800})
    outside: list[str] = []
    errors: list[str] = []

    def handle(route):
        url = route.request.url
        if url == f"{ORIGIN}/":
            route.fulfill(status=200, content_type="text/html", body=html)
        elif url.startswith(f"{ORIGIN}/public/"):
            name = url.removeprefix(f"{ORIGIN}/public/").split("?")[0]
            file = PUBLIC / name
            kind = {".js": "text/javascript", ".css": "text/css", ".png": "image/png"}[file.suffix]
            route.fulfill(status=200, content_type=kind, body=file.read_bytes())
        else:
            outside.append(url)
            route.abort()

    ctx.route("**/*", handle)
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{ORIGIN}/", wait_until="load")
    page.wait_for_timeout(150)
    page.outside, page.errors, page.ctx = outside, errors, ctx  # type: ignore[attr-defined]
    return page


def labels(page, selector: str) -> list[str]:
    return page.locator(selector).all_inner_texts()


def test_the_navigation_and_profile_are_inserted_once_in_the_right_order(browser) -> None:
    page = make_page(browser)
    assert page.locator("#eq-shell").count() == 1 and page.locator("#eq-profile").count() == 1
    assert labels(page, "#eq-shell .eq-row-label") == ["New chat", "Scheduled", "Library", "Plugins", "Explore", "Branded Residences"]
    assert labels(page, "#eq-shell .eq-heading") == ["Projects", "Recents"]
    assert page.locator("#eq-shell .eq-soon").count() == 4
    assert " ".join(page.locator("#eq-profile").inner_text().split()) == "DG DarGlobal Plus"
    assert page.errors == [] and page.outside == []           # no script errors, nothing fetched from elsewhere
    page.ctx.close()


def test_recents_sits_directly_above_the_conversation_list_and_profile_below_it(browser) -> None:
    page = make_page(browser)
    order = page.evaluate("""() => {
      const content = document.querySelector('[data-sidebar="content"]');
      const shell = document.getElementById('eq-shell');
      const history = document.getElementById('thread-history');
      return {
        shellIsFirst: content.firstElementChild === shell,
        recentsIsLastInShell: shell.lastElementChild.textContent === 'Recents',
        shellBeforeHistory: !!(shell.compareDocumentPosition(history) & Node.DOCUMENT_POSITION_FOLLOWING),
        profileAfterContent: content.nextElementSibling.id === 'eq-profile',
      };
    }""")
    assert all(order.values()), order
    page.ctx.close()


def test_new_chat_presses_chainlits_own_button_and_nothing_else_does(browser) -> None:
    page = make_page(browser)
    page.click("#eq-newchat")
    assert page.evaluate("window.newChatClicks") == 1
    for label in ("Scheduled", "Library", "Plugins", "Explore", "Branded Residences"):
        page.locator("#eq-shell .eq-row", has_text=label).click(force=True)
    page.click("#eq-profile")
    assert page.evaluate("window.newChatClicks") == 1          # the placeholders did not trigger it
    rows = page.locator("#eq-shell .eq-row[aria-disabled='true']")
    assert rows.count() == 5 and all(r.get_attribute("title") == "Coming soon" for r in rows.all())
    page.ctx.close()


def test_the_nodes_come_back_after_a_re_render_and_never_duplicate(browser) -> None:
    page = make_page(browser)
    # React replacing the sidebar's content element, as a re-render can
    page.evaluate("""() => {
      const old = document.querySelector('[data-sidebar="content"]');
      const fresh = document.createElement('div');
      fresh.setAttribute('data-sidebar', 'content');
      fresh.innerHTML = old.innerHTML.replace(/id="eq-shell"/, 'data-x');
      old.replaceWith(fresh);
    }""")
    page.wait_for_timeout(200)
    assert page.locator("#eq-shell").count() == 1 and page.locator("#eq-profile").count() == 1
    page.evaluate("document.getElementById('eq-shell').remove(); document.getElementById('eq-profile').remove()")
    page.wait_for_timeout(200)
    assert page.locator("#eq-shell").count() == 1 and page.locator("#eq-profile").count() == 1
    for _ in range(5):                                           # unrelated DOM churn changes nothing
        page.evaluate("document.body.append(document.createElement('span'))")
    page.wait_for_timeout(200)
    assert page.locator("#eq-shell").count() == 1 and page.locator("#eq-profile").count() == 1
    page.ctx.close()


def test_without_a_sidebar_the_script_does_nothing(browser) -> None:
    page = make_page(browser, NO_SIDEBAR)
    assert page.locator("#eq-shell").count() == 0 and page.locator("#eq-profile").count() == 0
    assert page.errors == []
    page.ctx.close()


def test_the_stylesheet_hides_and_replaces_what_chainlit_draws(browser) -> None:
    page = make_page(browser)
    state = page.evaluate("""() => {
      const shown = (sel) => { const e = document.querySelector(sel); return !!e && getComputedStyle(e).display !== 'none'; };
      return {
        groupLabelsVisible: [...document.querySelectorAll('#thread-history [data-sidebar="group-label"]')]
          .filter(e => getComputedStyle(e).display !== 'none').length,
        welcomeLogoShown: shown('#welcome-screen img.logo'),
        newChatIconShown: shown('#new-chat-button'),
        guestAvatarShown: shown('#user-nav-button'),
        greeting: getComputedStyle(document.getElementById('welcome-screen'), '::before').content,
        logoInHeader: getComputedStyle(document.querySelector('[data-sidebar="header"] > div'), '::before').backgroundImage,
        triggerOrder: getComputedStyle(document.getElementById('sidebar-trigger-button')).order,
      };
    }""")
    assert state["groupLabelsVisible"] == 0                      # one flat list under "Recents"
    assert not state["welcomeLogoShown"] and not state["newChatIconShown"] and not state["guestAvatarShown"]
    assert "How can I help, DarGlobal Team?" in state["greeting"]
    assert "/public/logo.png" in state["logoInHeader"]
    assert state["triggerOrder"] == "2"                          # the collapse button sits at the right end
    page.ctx.close()


def test_the_model_picker_files_load_the_shell() -> None:
    assert "/public/shell.js" in (PUBLIC / "model-picker.js").read_text(encoding="utf-8")
    assert "/public/shell.css" in (PUBLIC / "model-picker.css").read_text(encoding="utf-8")
    assert (PUBLIC / "model-picker.css").read_text(encoding="utf-8").lstrip().startswith("/*")
    first_rule = next(line for line in (PUBLIC / "model-picker.css").read_text(encoding="utf-8").splitlines()
                      if line.startswith(("@import", ":root", ".")))
    assert first_rule.startswith("@import")                      # @import is only valid before other rules
