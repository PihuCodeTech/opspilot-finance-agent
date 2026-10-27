"""The 3D layer in a real browser (`07-design.md` §11, `09-tasks.md` P13).

Every scene falls back to its 2D drawing with WebGL off, under reduced motion, below
768px and with the switch off; every scene is keyboard operable; nothing a scene does
writes anything. Read-only, so it runs before the journeys change the demo cases.
"""

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, Playwright, Request, expect, sync_playwright

from tests.e2e.conftest import PASSWORD, Server
from tests.e2e.test_journeys import SIGNED_IN, case_url, no_errors

pytestmark = pytest.mark.e2e

WEBGL = ["--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]  # software WebGL, headless
NO_WEBGL = ["--disable-webgl", "--disable-3d-apis"]


@pytest.fixture(scope="module")
def playwright() -> Iterator[Playwright]:
    with sync_playwright() as p:
        yield p


def _launch(p: Playwright, args: list[str]) -> Browser:
    try:
        return p.chromium.launch(channel="chrome", args=args)
    except Exception:  # noqa: BLE001  # no installed Chrome (CI): Playwright's Chromium
        return p.chromium.launch(args=args)


@pytest.fixture(scope="module")
def gl(playwright: Playwright) -> Iterator[Browser]:
    browser = _launch(playwright, WEBGL)
    yield browser
    browser.close()


@pytest.fixture(scope="module")
def no_gl(playwright: Playwright) -> Iterator[Browser]:
    browser = _launch(playwright, NO_WEBGL)
    yield browser
    browser.close()


def open_page(
    browser: Browser,
    server: Server,
    username: str,
    *,
    width: int = 1440,
    height: int = 900,
    reduced: bool = False,
    views3d: str | None = None,
) -> Page:
    """Signed in (one sign-in per user per session: login is rate-limited), with every
    request recorded so a test can show that no scene wrote anything."""
    context = browser.new_context(
        viewport={"width": width, "height": height},
        reduced_motion="reduce" if reduced else "no-preference",
        storage_state=SIGNED_IN.get(username),
    )
    if views3d:
        context.add_cookies([{"name": "views3d", "value": views3d, "url": server.url}])
    page = context.new_page()
    errors: list[str] = []
    requests: list[Request] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors  # type: ignore[attr-defined]  # read by no_errors
    page.sent = requests  # type: ignore[attr-defined]
    if username not in SIGNED_IN:
        page.goto(f"{server.url}/login")
        page.get_by_label("Username").fill(username)
        page.get_by_label("Password").fill(PASSWORD)
        page.get_by_role("button", name="Sign in").click()
        expect(page.get_by_role("heading", name="Cases", level=1)).to_be_visible()
        SIGNED_IN[username] = context.storage_state()
    page.on("request", lambda r: requests.append(r))  # noqa: PLW0108  # Playwright needs a Python callable
    return page


def visit(page: Page, url: str) -> None:
    """Open a page with its disclosures open and every scene scrolled past once."""
    page.goto(url)
    page.evaluate("document.querySelectorAll('section details').forEach(d => d.open = true)")
    for figure in page.locator("figure[data-scene]:visible").all():
        figure.scroll_into_view_if_needed()
        page.wait_for_timeout(400)


def live(page: Page, scene_id: str) -> None:
    figure = page.locator(f"#{scene_id}")
    figure.scroll_into_view_if_needed()
    expect(figure).to_have_class(re.compile(r"\bis-live\b"), timeout=5000)


def nothing_written(page: Page) -> None:
    """3D can't trigger a mutation: only GETs, and no script-initiated requests at all."""
    sent: list[Request] = page.sent  # type: ignore[attr-defined]
    assert [r.url for r in sent if r.method != "GET"] == []
    scripted = [
        r.url for r in sent if r.resource_type in {"fetch", "xhr"} and "/_progress" not in r.url
    ]
    assert scripted == []


def status(page: Page, scene_id: str) -> str:
    return page.locator(f"#{scene_id} .scene-stage p[aria-live]").inner_text()


def tab_into(page: Page, scene_id: str, times: int = 1) -> None:
    page.locator(f"#{scene_id} canvas").focus()
    for _ in range(times):
        page.keyboard.press("Tab")


# ---------------------------------------------------------------- the 2D fallbacks


FALLBACKS: dict[str, dict[str, Any]] = {
    "webgl off": {"browser": "no_gl"},
    "reduced motion": {"browser": "gl", "reduced": True},
    "below 768px": {"browser": "gl", "width": 700},
    "switched off": {"browser": "gl", "views3d": "off"},
}


@pytest.mark.parametrize("why", list(FALLBACKS))
def test_every_scene_falls_back_to_the_same_data_in_2d(
    request: pytest.FixtureRequest, server: Server, why: str
) -> None:
    options = dict(FALLBACKS[why])
    browser: Browser = request.getfixturevalue(options.pop("browser"))
    page = open_page(browser, server, "priya.raman", **options)
    for url in [server.url + "/", case_url(server, "price") + "?evidence=3d"]:
        visit(page, url)
        page.wait_for_timeout(600)
        assert page.locator("figure.is-live, figure[data-scene] canvas").count() == 0, url
        for figure in page.locator("figure[data-scene]:visible").all():
            fallback = figure.locator(".scene-fallback")
            expect(fallback).to_be_visible()
            payload = json.loads(figure.locator("[data-scene-payload]").text_content() or "{}")
            label = fallback.locator("[aria-label]").first.get_attribute("aria-label")
            assert label == payload["label"], figure.get_attribute("id")
    # The same figures, in words the fallback shows: the queue's piles and the price break.
    visit(page, server.url + "/")
    expect(page.locator("#desk .desk-bars")).to_contain_text("Needs review")
    visit(page, case_url(server, "price") + "?evidence=3d")
    expect(page.locator("#match-stack .scene-fallback")).to_contain_text("Price +7.14%")
    if why == "switched off":  # and no 3D code is even downloaded
        assert not [r.url for r in page.sent if "three" in r.url or "stage3d" in r.url]  # type: ignore[attr-defined]
    nothing_written(page)
    no_errors(page)


def test_the_hero_stays_flat_under_reduced_motion_even_when_3d_is_turned_on(
    gl: Browser, server: Server
) -> None:
    page = open_page(gl, server, "priya.raman", reduced=True, views3d="on")
    # 3D turned on explicitly allows static scenes (§11.5) ...
    visit(page, case_url(server, "price") + "?evidence=3d")
    live(page, "match-stack")
    # ... but never the idle-rotating hero.
    context = gl.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
    context.add_cookies([{"name": "views3d", "value": "on", "url": server.url}])
    login = context.new_page()
    login.goto(server.url + "/login")
    login.wait_for_timeout(800)
    expect(login.locator("#hero")).not_to_have_class(re.compile(r"\bis-live\b"))
    expect(login.locator("#hero .scene-fallback svg")).to_be_visible()
    context.close()


# ---------------------------------------------------------------- 3D, by keyboard


def test_the_match_stack_selects_the_row_and_its_finding(gl: Browser, server: Server) -> None:
    page = open_page(gl, server, "priya.raman")
    visit(page, case_url(server, "price") + "?evidence=3d")
    live(page, "match-stack")
    canvas = page.locator("#match-stack canvas")
    expect(canvas).to_have_attribute("role", "img")
    assert "Price +7.14%" in (canvas.get_attribute("aria-label") or "")
    tab_into(page, "match-stack")
    assert status(page, "match-stack").startswith("Line 1, ")
    assert "Price +7.14%" in status(page, "match-stack")
    for key in ["ArrowLeft", "ArrowUp", "+", "-"]:  # orbit and zoom, within limits
        page.keyboard.press(key)
    page.keyboard.press("Enter")
    expect(page.locator("#strip-line-1")).to_have_class(re.compile("scene-marked"))
    finding = page.locator("article.finding.scene-selected")
    expect(finding).to_be_focused()
    # Tab past the last line leaves the scene.
    tab_into(page, "match-stack", times=3)
    expect(canvas).not_to_be_focused()
    nothing_written(page)
    no_errors(page)


def test_the_table_is_the_default_and_3d_only_adds_a_view_below_it(
    gl: Browser, server: Server
) -> None:
    page = open_page(gl, server, "priya.raman")
    visit(page, case_url(server, "price"))
    table = page.get_by_role("table", name="Each invoice line against its PO line")
    expect(table).to_be_visible()  # the default, authoritative view
    expect(page.locator("#match-stack")).to_be_hidden()
    toggle = page.get_by_role("button", name="Show 3D view")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    toggle.click()
    live(page, "match-stack")
    expect(table).to_be_visible()  # 3D never replaces the table
    expect(page.get_by_role("button", name="Hide 3D view")).to_have_attribute(
        "aria-pressed", "true"
    )
    page.get_by_role("button", name="Hide 3D view").click()
    expect(page.locator("#match-stack")).to_be_hidden()
    nothing_written(page)
    no_errors(page)


def test_the_document_fan_opens_a_page_in_the_viewer(gl: Browser, server: Server) -> None:
    page = open_page(gl, server, "priya.raman")
    visit(page, case_url(server, "price") + "?evidence=3d")
    live(page, "document-fan")
    tab_into(page, "document-fan")
    assert status(page, "document-fan").startswith("Invoice page 1")
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"\?page=1#documents$"))
    nothing_written(page)
    no_errors(page)


def test_the_pipeline_track_opens_a_steps_trace_row(gl: Browser, server: Server) -> None:
    page = open_page(gl, server, "priya.raman")
    visit(page, case_url(server, "price") + "?evidence=3d")
    live(page, "pipeline")
    tab_into(page, "pipeline", times=2)
    assert status(page, "pipeline").startswith("Extract")
    page.keyboard.press("Enter")
    expect(page.locator("#trace-step-extract")).to_be_focused()
    nothing_written(page)
    no_errors(page)


def test_the_audit_chain_opens_an_events_row(gl: Browser, server: Server) -> None:
    page = open_page(gl, server, "ines.duarte")
    visit(page, case_url(server, "paid"))
    live(page, "audit-chain")
    tab_into(page, "audit-chain")
    assert re.match(
        r"Event \d+: .*, hash [0-9a-f]{8}, (verified|unchecked)", status(page, "audit-chain")
    )
    page.keyboard.press("Enter")
    focused = page.evaluate("document.activeElement.id")
    assert focused.startswith("audit-")
    nothing_written(page)
    no_errors(page)


def test_the_desk_filters_the_queue(gl: Browser, server: Server) -> None:
    page = open_page(gl, server, "priya.raman")
    visit(page, server.url + "/")
    live(page, "desk")
    tab_into(page, "desk")
    assert re.match(r"Needs review: \d+\. Enter shows them in the table\.", status(page, "desk"))
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"/\?status=needs_review$"))
    nothing_written(page)
    no_errors(page)


def test_the_resolution_chain_follows_the_checkbox_and_never_ticks_it(
    gl: Browser, server: Server
) -> None:
    page = open_page(gl, server, "sam.okafor")
    visit(page, case_url(server, "resolved"))
    page.locator("#resolution-chain").scroll_into_view_if_needed()
    live(page, "resolution-chain")
    tab_into(page, "resolution-chain")
    assert status(page, "resolution-chain").endswith("Not accepted yet.")
    page.keyboard.press("Enter")
    box = page.locator("input[data-accept]")
    expect(box).to_be_focused()
    expect(box).not_to_be_checked()  # the scene only points at the checkbox
    expect(page.locator("button[data-approve]")).to_be_disabled()
    box.check()  # in HTML, by the approver
    expect(page.locator("[data-accept-link]")).to_have_class(re.compile("accepted"))
    tab_into(page, "resolution-chain")
    assert status(page, "resolution-chain").endswith("Accepted.")
    nothing_written(page)
    no_errors(page)


# ---------------------------------------------------------------- the stage's own rules


def test_no_more_than_two_scenes_are_live_and_the_layout_does_not_move(
    gl: Browser, no_gl: Browser, server: Server
) -> None:
    flat = open_page(no_gl, server, "priya.raman", height=5000)
    visit(flat, case_url(server, "price") + "?evidence=3d")
    heights = flat.evaluate(
        "Object.fromEntries([...document.querySelectorAll('figure[data-scene]')]"
        ".map(f => [f.id, f.getBoundingClientRect().height]))"
    )
    page = open_page(gl, server, "priya.raman", height=5000)  # every scene in view at once
    visit(page, case_url(server, "price") + "?evidence=3d")
    page.wait_for_timeout(1500)
    assert page.locator("figure.is-live").count() == 2
    assert page.locator("figure[data-scene]:visible").count() >= 3  # the rest stay flat
    for figure in page.locator("figure.is-live").all():
        scene_id = figure.get_attribute("id")
        canvas = figure.locator("canvas").bounding_box()
        drawing = figure.locator(".scene-fallback").bounding_box()
        assert canvas and drawing and abs(canvas["height"] - drawing["height"]) < 1
        assert abs(figure.bounding_box()["height"] - heights[scene_id]) < 1  # type: ignore[index]
    no_errors(page)


def test_a_lost_context_falls_back_to_2d(gl: Browser, server: Server) -> None:
    page = open_page(gl, server, "priya.raman")
    visit(page, case_url(server, "price") + "?evidence=3d")
    live(page, "match-stack")
    page.evaluate(
        "document.querySelector('#match-stack canvas').getContext('webgl2')"
        ".getExtension('WEBGL_lose_context').loseContext()"
    )
    expect(page.locator("#match-stack")).not_to_have_class(re.compile(r"\bis-live\b"))
    expect(page.locator("#match-stack .scene-fallback")).to_be_visible()
    expect(page.locator("#match-stack canvas")).to_have_count(0)


def test_the_live_track_keeps_its_canvas_while_progress_polls(gl: Browser, server: Server) -> None:
    page = open_page(gl, server, "priya.raman")
    page.goto(case_url(server, "investigating"))
    live(page, "pipeline-live")
    page.evaluate("window.__canvas = document.querySelector('#pipeline-live canvas')")
    with page.expect_response(re.compile("/_progress")):
        pass  # one poll, whose data and fallback arrive out of band
    page.wait_for_timeout(300)
    assert page.evaluate("document.querySelector('#pipeline-live canvas') === window.__canvas")
    expect(page.locator("#pipeline-live")).to_have_class(re.compile(r"\bis-live\b"))
    assert "Investigation in progress" in (
        page.locator("#pipeline-live canvas").get_attribute("aria-label") or ""
    )
    nothing_written(page)
    no_errors(page)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_scene_draws_in_both_themes(gl: Browser, server: Server, theme: str) -> None:
    for username, where, scenes in [
        ("priya.raman", "/", ["desk"]),
        ("priya.raman", "price", ["match-stack", "document-fan", "pipeline", "audit-chain"]),
        ("sam.okafor", "resolved", ["resolution-chain"]),
    ]:
        page = open_page(gl, server, username)
        page.context.add_cookies([{"name": "theme", "value": theme, "url": server.url}])
        visit(
            page,
            server.url + where
            if where.startswith("/")
            else case_url(server, where) + "?evidence=3d",
        )
        assert page.evaluate("document.documentElement.dataset.theme") == theme
        for scene_id in scenes:
            live(page, scene_id)
        nothing_written(page)
        no_errors(page)
        page.context.close()
    login = gl.new_context(viewport={"width": 1440, "height": 900})
    login.add_cookies([{"name": "theme", "value": theme, "url": server.url}])
    hero = login.new_page()
    hero.goto(server.url + "/login")
    live(hero, "hero")
    expect(hero.locator("#hero canvas")).to_have_attribute("aria-hidden", "true")
    login.close()
