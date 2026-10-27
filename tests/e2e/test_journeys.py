"""J1–J4 in a real browser (`04-prd.md` §4, `09-tasks.md` P12), both themes, and by keyboard.

The journeys change the demo cases, so each case is used by one journey only. The
theme smoke test is read-only and visits every journey's screens in light and dark.
"""

import re
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Browser, FilePayload, Page, expect, sync_playwright

from tests.e2e.conftest import PASSWORD, Server
from tests.integration.web_support import text_pdf

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        try:
            launched = p.chromium.launch(channel="chrome")
        except Exception:  # noqa: BLE001  # no installed Chrome (CI): Playwright's Chromium
            launched = p.chromium.launch()
        yield launched
        launched.close()


SIGNED_IN: dict[str, Any] = {}  # one sign-in per user per session: login is rate-limited


def open_as(browser: Browser, server: Server, username: str, theme: str = "light") -> Page:
    context = browser.new_context(
        viewport={"width": 1440, "height": 900}, storage_state=SIGNED_IN.get(username)
    )
    context.add_cookies([{"name": "theme", "value": theme, "url": server.url}])
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors  # type: ignore[attr-defined]  # read by the tests below
    if username not in SIGNED_IN:
        page.goto(f"{server.url}/login")
        page.get_by_label("Username").fill(username)
        page.get_by_label("Password").fill(PASSWORD)
        page.get_by_role("button", name="Sign in").click()
        expect(page.get_by_role("heading", name="Cases", level=1)).to_be_visible()
        SIGNED_IN[username] = context.storage_state()
    else:
        page.goto(server.url + "/")
    return page


def no_errors(page: Page) -> None:
    errors: list[str] = page.errors  # type: ignore[attr-defined]
    assert errors == [], errors  # no CSP violations, no script errors


def case_url(server: Server, key: str) -> str:
    return f"{server.url}/cases/{server.demo.cases[key]}"


# ---------------------------------------------------------------- both themes


SCREENS = {
    "priya.raman": ["/", "price", "clean", "restricted", "inconclusive", "failed", "/uploads/new"],
    "sam.okafor": ["/approvals", "controller", "/approvals?status=held_for_approver"],
    "ines.duarte": ["/", "paid"],
}


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_journey_screen_renders_in_both_themes(
    browser: Browser, server: Server, theme: str
) -> None:
    for username, screens in SCREENS.items():
        page = open_as(browser, server, username, theme)
        assert page.locator("html").get_attribute("data-theme") == theme
        ground = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert ground == ("rgb(242, 244, 247)" if theme == "light" else "rgb(14, 15, 16)")
        for screen in screens:
            page.goto(server.url + screen if screen.startswith("/") else case_url(server, screen))
            expect(page.locator("h1")).to_have_count(1)
            assert page.locator("main#main").count() == 1
        no_errors(page)
        page.context.close()


# ---------------------------------------------------------------- J1: a flagged invoice


def test_j1_analyst_investigates_a_flagged_invoice(
    browser: Browser, server: Server, tmp_path: object
) -> None:
    page = open_as(browser, server, "priya.raman")
    page.get_by_role("link", name="INV-24-0418", exact=True).click()
    expect(page.get_by_text("1 exception found · 0 resolved · payment blocked")).to_be_visible()
    finding = page.locator("article.finding").first
    expect(finding.get_by_role("heading", name=re.compile("Price above PO"))).to_be_visible()
    expect(finding.locator("table.evidence")).to_contain_text("+7.14%")
    expect(finding.locator("blockquote")).to_contain_text("Policy §")
    # Approve payment isn't offered while the finding is unresolved, and says why.
    expect(page.get_by_text("Approve payment unavailable: 1 finding unresolved.")).to_be_visible()

    # Add the credit note, then record the resolution it supports.
    page.get_by_text("Add supporting document").click()
    page.get_by_label("Kind").select_option("credit_note")
    page.get_by_label("Description").fill("Credit note CN-4410 for 3.60")
    pdf = text_pdf(("Credit note CN-4410 3.60", 72.0, 700.0))
    page.get_by_label("File (PDF, PNG or JPEG)").set_input_files(
        files=[FilePayload(name="cn.pdf", mimeType="application/pdf", buffer=pdf)]
    )
    page.get_by_role("button", name="Attach document").click()
    expect(page.get_by_role("link", name="Credit note", exact=True)).to_be_visible()  # its download

    finding = page.locator("article.finding").first
    finding.get_by_text("Resolve", exact=True).click()
    credit = finding.locator("form", has=page.get_by_text("Credit issued", exact=True))
    credit.get_by_label("Credit amount").fill("3.60")
    credit.get_by_label("Summary (not sufficient on its own)").fill("Credit note covers line 1")
    credit.get_by_role("button", name="Record resolution").click()
    assert page.locator("[role=alert]").count() == 0  # no error came back
    expect(
        page.locator("article.finding").first.locator(".pill", has_text="Resolved")
    ).to_be_visible()
    expect(page.get_by_text("0 unresolved")).to_be_visible()

    # Now payment may be proposed, with a reason, and submitted.
    page.get_by_label("Approve payment").check()
    page.get_by_label("Reason").fill("Overcharge credited by CN-4410")
    page.get_by_role("button", name="Save proposal").click()
    expect(page.locator(".proposal .action")).to_have_text("Approve payment")
    page.get_by_role("button", name=re.compile("Submit payment for approval")).click()
    expect(page.locator(".title-row .pill")).to_contain_text("Waiting for approver")
    no_errors(page)


def test_j1_a_stale_form_comes_back_with_its_reason(browser: Browser, server: Server) -> None:
    page = open_as(browser, server, "priya.raman")
    page.goto(case_url(server, "restricted"))
    other = open_as(browser, server, "tomas.berg")
    other.goto(case_url(server, "restricted"))
    other.get_by_label("Vendor query").check()
    other.get_by_label("Reason").fill("Ask the vendor about the new account")
    other.get_by_role("button", name="Save proposal").click()
    page.get_by_label("Reject invoice").check()
    page.get_by_label("Reason").fill("Looks fraudulent")
    page.get_by_role("button", name="Save proposal").click()
    expect(page.get_by_role("alert")).to_contain_text("Someone changed this case")
    expect(page.locator(".proposal .action")).to_have_text("Vendor query")


# ---------------------------------------------------------------- J2: a clean invoice


def test_j2_analyst_confirms_a_clean_invoice(browser: Browser, server: Server) -> None:
    page = open_as(browser, server, "priya.raman")
    page.goto(case_url(server, "clean"))
    expect(page.get_by_text(re.compile(r"All \d+ checks passed"))).to_be_visible()
    expect(page.get_by_text("No findings: every check passed.")).to_be_visible()
    page.get_by_role(
        "button", name=re.compile(r"Submit payment for approval \(\$654\.39\)")
    ).click()
    expect(page.locator(".title-row .pill")).to_contain_text("Waiting for approver")


# ---------------------------------------------------------------- J3: the approver


def test_j3_approver_accepts_every_resolution_then_approves(
    browser: Browser, server: Server
) -> None:
    page = open_as(browser, server, "sam.okafor")
    page.get_by_role("link", name=re.compile("^Approvals")).click()
    page.get_by_role("link", name="INV-24-0431", exact=True).click()
    approve = page.get_by_role("button", name="Approve payment of $175.37")
    expect(approve).to_be_disabled()  # blocked until every resolution is accepted
    expect(page.get_by_text("0 of 1 accepted. Accept all 1 to approve.")).to_be_visible()
    page.get_by_label(re.compile("Accept resolution: Credit issued")).check()
    expect(approve).to_be_enabled()
    approve.click()
    expect(page.locator(".title-row .pill")).to_contain_text("Executing")
    no_errors(page)


def test_j3_the_server_refuses_approval_without_acceptance(
    browser: Browser, server: Server
) -> None:
    """The UI lock isn't the safeguard: a forged submit without acceptances is refused."""
    page = open_as(browser, server, "lena.hartmann")
    page.goto(case_url(server, "clean"))  # submitted for payment in J2, if it ran first
    if page.locator("form[data-review]").count() == 0:
        pytest.skip("the clean case wasn't submitted in this session")
    page.evaluate(
        "document.querySelectorAll('[data-accept]').forEach(b => b.removeAttribute('required'));"
        "const f = document.querySelector('form[data-review]');"
        "f.querySelector('[data-approve]').disabled = false;"
    )
    page.get_by_role("button", name=re.compile("Approve payment of")).click()
    expect(page.locator(".title-row .pill")).to_contain_text("Executing")  # nothing to accept


def test_j3_over_limit_is_explained_and_decline_stays_available(
    browser: Browser, server: Server
) -> None:
    page = open_as(browser, server, "sam.okafor")
    page.goto(case_url(server, "controller"))
    expect(page.get_by_role("button", name="Approve")).to_be_disabled()
    expect(page.get_by_text("This case needs a controller.")).to_be_visible()
    page.get_by_text("Decline with reason").click()
    page.get_by_label("Why decline").fill("Inspection report missing")
    page.get_by_role("button", name="Decline and return to the analyst").click()
    expect(page.locator(".title-row .pill")).to_contain_text("Needs review")


# ---------------------------------------------------------------- J4: the auditor


def test_j4_auditor_reads_a_closed_case_and_can_change_nothing(
    browser: Browser, server: Server
) -> None:
    page = open_as(browser, server, "ines.duarte")
    page.goto(case_url(server, "paid"))
    expect(page.locator(".title-row .pill")).to_contain_text("Paid")
    expect(page.get_by_text(re.compile("Paid by approval of Sam Okafor"))).to_be_visible()
    forms = page.locator("main form[method=post]")
    expect(forms).to_have_count(0)  # no edit controls at all
    page.get_by_role("heading", name="Audit trail").click()
    expect(page.get_by_text(re.compile("Chain verified"))).to_be_visible()
    expect(page.get_by_text(re.compile("tamper-evident, not tamper-proof"))).to_be_visible()
    page.get_by_role("heading", name="Run trace").click()
    expect(page.get_by_text(re.compile("Run 1 \\(investigation\\)"))).to_be_visible()
    no_errors(page)


# ---------------------------------------------------------------- keyboard only


def tab_to(page: Page, name: str = "", *, element_id: str = "", limit: int = 80) -> None:
    """Press Tab until the named control (or the element with this id) has focus."""
    for _ in range(limit):
        page.keyboard.press("Tab")
        focused = page.evaluate(
            "(() => { const e = document.activeElement; return [e.id, (e.getAttribute('aria-label')"
            " || e.innerText || e.value || '').trim()]; })()"
        )
        if (element_id and focused[0] == element_id) or (name and focused[1].startswith(name)):
            return
    raise AssertionError(f"{name or element_id!r} not reachable by Tab")


def test_keyboard_walkthrough_queue_case_resolve_submit_approve(
    browser: Browser, server: Server
) -> None:
    page = open_as(browser, server, "priya.raman")
    # The queue: Tab into the table, arrows to a row, Enter opens it.
    page.goto(f"{server.url}/?status=needs_review&q=MK-30954")
    page.locator("table[data-queue]").focus()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator("h1")).to_contain_text("MK-30954")
    # An inconclusive finding offers a recheck instead of a resolution.
    expect(page.get_by_text("Only a recheck can clear this finding")).to_be_visible()
    tab_to(page, "Request recheck")
    page.keyboard.press("Enter")
    tab_to(page, element_id="recheck-reason")
    page.keyboard.type("Clearer copy requested from the vendor")
    tab_to(page, "Recheck the invoice")
    page.keyboard.press("Enter")
    expect(page.locator(".title-row .pill")).to_contain_text("Investigating")
    # Focus is always visible: a 2px outline on whatever has focus.
    page.keyboard.press("Tab")
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineWidth")
    assert outline == "2px"
