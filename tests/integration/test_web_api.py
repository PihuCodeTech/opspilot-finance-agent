"""The HTTP contract of `06-api.md` over the real app and database (P11)."""

import io
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import Connection, text

from opspilot.cases import service
from opspilot.domain.bank import fingerprint
from opspilot.domain.enums import FindingCode, SupportingKind
from opspilot.web.auth import SESSION_COOKIE
from opspilot.web.pages import REDACTION_COLOUR
from tests.integration import case_support as cs
from tests.integration import factories as f
from tests.integration.web_support import (
    PASSWORD,
    Client,
    Web,
    make_web,
    parse_row,
    pdf_bytes,
    png_bytes,
    text_pdf,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def web(conn: Connection, tmp_path: Path) -> Web:
    return make_web(conn, tmp_path)


def _error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == code, error
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert set(error) == {"code", "message", "details", "request_id"}
    return error


# ---------------------------------------------------------------- sessions


def test_sign_in_sets_a_hardened_cookie_and_stores_only_a_hash(web: Web) -> None:
    client = web.client()
    response = client.sign_in(web.username(web.world.analyst))
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert all(flag in cookie for flag in ("HttpOnly", "SameSite=lax", "Path=/", "Secure"))
    token = client.cookies[SESSION_COOKIE]
    stored = web.world.conn.execute(text("SELECT token_hash FROM sessions")).scalars().all()
    assert token.encode() not in [bytes(s) for s in stored]
    me = client.get("/api/v1/me").json()
    assert me["role"] == "analyst" and me["id"] == str(web.world.analyst)
    assert me["csrf_token"] == client.csrf


def test_wrong_credentials_get_one_generic_answer(web: Web) -> None:
    client = web.client()
    username = web.username(web.world.analyst)
    wrong = _error(client.sign_in(username, "nope"), 401, "unauthenticated")
    unknown = _error(client.sign_in("nobody-here"), 401, "unauthenticated")
    assert wrong["message"] == unknown["message"]
    web.world.conn.execute(
        text("UPDATE users SET is_active = false WHERE id = :u"), {"u": web.world.analyst2}
    )
    _error(client.sign_in(web.username(web.world.analyst2)), 401, "unauthenticated")
    assert SESSION_COOKIE not in client.cookies


def test_logout_ends_the_session(web: Web) -> None:
    client = web.client(web.world.analyst)
    assert client.mutate("POST", "/auth/logout").status_code == 204
    _error(client.get("/api/v1/me"), 401, "unauthenticated")


def test_idle_sessions_expire(web: Web) -> None:
    client = web.client(web.world.analyst)
    web.world.conn.execute(text("UPDATE sessions SET expires_at = now() - interval '1 minute'"))
    _error(client.get("/api/v1/me"), 401, "unauthenticated")
    left = web.world.conn.execute(text("SELECT count(*) FROM sessions")).scalar_one()
    assert left == 0


def test_requests_slide_the_idle_expiry(web: Web) -> None:
    client = web.client(web.world.analyst)
    web.world.conn.execute(
        text(
            "UPDATE sessions SET last_seen_at = now() - interval '2 hours',"
            " expires_at = now() + interval '10 minutes'"
        )
    )
    assert client.get("/api/v1/me").status_code == 200
    hours = web.world.conn.execute(
        text("SELECT extract(epoch FROM expires_at - now()) / 3600 FROM sessions")
    ).scalar_one()
    assert hours > 7  # SESSION_IDLE_HOURS is 8


def test_login_is_rate_limited(web: Web) -> None:
    client = web.client()
    username = web.username(web.world.analyst)
    for _ in range(5):
        _error(client.sign_in(username, "wrong"), 401, "unauthenticated")
    limited = client.sign_in(username, PASSWORD)  # even the right password waits
    _error(limited, 429, "rate_limited")
    assert int(limited.headers["Retry-After"]) > 0
    web.clock.now += 61
    assert client.sign_in(username).status_code == 204


def test_cross_site_sign_in_is_refused(web: Web) -> None:
    client = web.client()
    response = client.post(
        "/auth/login",
        json={"username": web.username(web.world.analyst), "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    _error(response, 403, "csrf_failed")


def test_unauthenticated_json_gets_401_and_html_redirects(web: Web) -> None:
    client = web.client()
    _error(client.get("/api/v1/cases"), 401, "unauthenticated")
    page = client.get("/")
    assert page.status_code == 303 and page.headers["location"] == "/login"


# ---------------------------------------------------------------- CSRF and permissions


def _needs_review(web: Web) -> tuple[uuid.UUID, uuid.UUID]:
    case_id = web.world.case([cs.approval(), cs.price()])
    return case_id, web.world.finding(case_id, FindingCode.PRICE_MISMATCH)


def _mutations(
    web: Web, case_id: uuid.UUID, finding_id: uuid.UUID
) -> dict[str, tuple[str, str, Callable[[Client], dict[str, Any]], set[str]]]:
    """Every mutating endpoint: method, path, request arguments, and the roles allowed."""
    base = f"/api/v1/cases/{case_id}"

    def versioned(**extra: Any) -> Callable[[Client], dict[str, Any]]:
        return lambda c: {"json": {"expected_version": c.version(case_id), **extra}}

    def upload(**fields: str) -> Callable[[Client], dict[str, Any]]:
        return lambda _c: {
            "files": {"file": ("x.pdf", pdf_bytes(), "application/pdf")},
            "data": fields,
        }

    analyst, approver = {"analyst"}, {"approver", "controller"}
    return {
        "create case": (
            "POST",
            "/api/v1/cases",
            lambda _c: {"json": {"invoice_id": str(f.invoice(web.world.conn, web.world.dataset))}},
            analyst,
        ),
        "retry": ("POST", f"{base}/investigate", versioned(), analyst),
        "change proposal": (
            "PUT",
            f"{base}/proposal",
            versioned(action="place_on_hold", reason="hold it"),
            analyst,
        ),
        "submit": ("POST", f"{base}/submit", versioned(), analyst),
        "dismiss": ("POST", f"{base}/dismiss", versioned(reason="not ours"), analyst),
        "approve": ("POST", f"{base}/approve", versioned(), approver),
        "decline": ("POST", f"{base}/decline", versioned(reason="check again"), approver),
        "reopen": ("POST", f"{base}/reopen", versioned(reason="mistake"), approver),
        "supporting document": (
            "POST",
            f"{base}/supporting-documents",
            upload(kind="credit_note", description="credit note"),
            analyst,
        ),
        "bank verification": (
            "POST",
            f"{base}/bank-verifications",
            versioned(
                channel="callback_to_master_contact",
                contact_source="vendor_master",
                contact_reference="master phone",
                verified_account_number=cs.CHANGED_ACCOUNT,
                evidence_document_id=str(uuid.uuid4()),
            ),
            analyst,
        ),
        "resolution": (
            "POST",
            f"{base}/findings/{finding_id}/resolutions",
            versioned(resolution_type="credit_issued", summary="credit", evidence=[]),
            analyst,
        ),
        "recheck": ("POST", f"{base}/recheck", versioned(reason="clearer copy"), analyst),
        "upload": ("POST", "/api/v1/uploads", upload(), analyst),
    }


def test_every_mutation_requires_the_csrf_token(web: Web) -> None:
    case_id, finding_id = _needs_review(web)
    client = web.client(web.world.analyst)
    for name, (method, path, args, _roles) in _mutations(web, case_id, finding_id).items():
        missing = client.request(method, path, **args(client))
        _error(missing, 403, "csrf_failed")
        forged = client.request(method, path, headers={"X-CSRF-Token": "forged"}, **args(client))
        assert forged.status_code == 403, name
    _error(client.request("POST", "/auth/logout"), 403, "csrf_failed")
    assert client.get("/api/v1/me").status_code == 200  # still signed in


def test_permission_matrix(web: Web) -> None:
    case_id, finding_id = _needs_review(web)
    w = web.world
    users = {"analyst": w.analyst, "approver": w.approver, "controller": w.controller}
    users["auditor"] = w.auditor
    clients = {role: web.client(user) for role, user in users.items()}
    for name, (method, path, args, roles) in _mutations(web, case_id, finding_id).items():
        for role, client in clients.items():
            response = client.mutate(method, path, **args(client))
            if role in roles:
                assert response.status_code != 403, (name, role, response.text)
            else:
                _error(response, 403, "forbidden")


def test_reads_by_role(web: Web) -> None:
    case_id, _ = _needs_review(web)
    w = web.world
    for user in (w.analyst, w.approver, w.auditor):
        client = web.client(user)
        assert client.get(f"/api/v1/cases/{case_id}").status_code == 200
        assert client.get(f"/api/v1/cases/{case_id}/audit").status_code == 200
        assert client.get("/api/v1/cases").status_code == 200
    _error(web.client(w.analyst).get("/api/v1/approvals"), 403, "forbidden")
    assert web.client(w.approver).get("/api/v1/approvals").status_code == 200


# ---------------------------------------------------------------- errors and shapes


def test_error_codes_and_shapes(web: Web) -> None:
    case_id, _ = _needs_review(web)
    client = web.client(web.world.analyst)
    version = client.version(case_id)
    stale = client.mutate(
        "POST", f"/api/v1/cases/{case_id}/dismiss", json={"reason": "x", "expected_version": 99}
    )
    _error(stale, 409, "stale_version")
    _error(
        client.mutate(
            "POST", f"/api/v1/cases/{case_id}/investigate", json={"expected_version": version}
        ),
        409,
        "invalid_transition",
    )
    blank = client.mutate(
        "POST",
        f"/api/v1/cases/{case_id}/dismiss",
        json={"reason": "  ", "expected_version": version},
    )
    _error(blank, 422, "validation_failed")
    bad = client.mutate("POST", f"/api/v1/cases/{case_id}/dismiss", json={"expected_version": "x"})
    fields = _error(bad, 422, "validation_failed")["details"]["fields"]
    assert {f["field"] for f in fields} == {"reason", "expected_version"}
    extra = client.mutate(
        "POST",
        f"/api/v1/cases/{case_id}/dismiss",
        json={"reason": "x", "expected_version": version, "force": True},
    )
    _error(extra, 422, "validation_failed")  # extra = forbid
    malformed = client.mutate(
        "POST",
        f"/api/v1/cases/{case_id}/dismiss",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    _error(malformed, 400, "bad_request")
    _error(client.get(f"/api/v1/cases/{uuid.uuid4()}"), 404, "not_found")
    _error(client.get("/api/v1/cases/not-a-uuid"), 404, "not_found")
    _error(client.get("/api/v1/nothing-here"), 404, "not_found")
    proposal = client.mutate(
        "PUT",
        f"/api/v1/cases/{case_id}/proposal",
        json={"action": "approve_for_payment", "reason": "pay", "expected_version": version},
    )
    assert proposal.status_code in {403, 409}  # an unresolved finding blocks payment


def test_validation_errors_never_echo_input(web: Web) -> None:
    case_id = web.world.case([cs.approval(), cs.bank_change()])
    client = web.client(web.world.analyst)
    response = client.mutate(
        "POST",
        f"/api/v1/cases/{case_id}/bank-verifications",
        json={"verified_account_number": cs.CHANGED_ACCOUNT, "expected_version": "x"},
    )
    _error(response, 422, "validation_failed")
    assert cs.CHANGED_ACCOUNT not in response.text


def test_money_is_a_string_and_versions_drive_concurrency(web: Web) -> None:
    case_id, _ = _needs_review(web)
    analyst = web.client(web.world.analyst)
    version = analyst.version(case_id)
    changed = analyst.mutate(
        "PUT",
        f"/api/v1/cases/{case_id}/proposal",
        json={"action": "draft_vendor_query", "reason": "ask", "expected_version": version},
    )
    assert changed.status_code == 200 and changed.json()["version"] == version + 1
    submitted = analyst.mutate(
        "POST", f"/api/v1/cases/{case_id}/submit", json={"expected_version": version + 1}
    )
    assert submitted.status_code == 200
    detail = submitted.json()
    assert detail["status"] == "pending_approval"
    assert detail["required_approval_amount"] == "5000.00"


# ---------------------------------------------------------------- approval over HTTP


def test_approval_queue_and_approve(web: Web) -> None:
    w = web.world
    case_id = w.case([cs.approval()])  # clean: the agent proposes payment
    analyst = web.client(w.analyst)
    analyst.mutate(
        "POST",
        f"/api/v1/cases/{case_id}/submit",
        json={"expected_version": analyst.version(case_id)},
    )
    approver = web.client(w.approver)
    queue = approver.get("/api/v1/approvals").json()["items"]
    assert [c["id"] for c in queue] == [str(case_id)]
    big = w.case([cs.approval("50000.00")])
    analyst.mutate(
        "POST", f"/api/v1/cases/{big}/submit", json={"expected_version": analyst.version(big)}
    )
    assert [c["id"] for c in approver.get("/api/v1/approvals").json()["items"]] == [str(case_id)]
    controller_queue = web.client(w.controller).get("/api/v1/approvals").json()["items"]
    assert {c["id"] for c in controller_queue} == {str(case_id), str(big)}
    detail = approver.get(f"/api/v1/cases/{case_id}").json()
    assert "approve" in detail["operations"]
    assert "approve" not in web.client(w.approver).get(f"/api/v1/cases/{big}").json()["operations"]
    approved = approver.mutate(
        "POST", f"/api/v1/cases/{case_id}/approve", json={"expected_version": detail["version"]}
    )
    assert approved.status_code == 202 and approved.json()["status"] == "approved"


# ---------------------------------------------------------------- pagination


def test_cursor_pagination_is_stable(web: Web) -> None:
    w = web.world
    created = [w.case([cs.approval()]) for _ in range(7)]
    client = web.client(w.analyst)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        page = client.get("/api/v1/cases", params=params).json()
        seen += [c["id"] for c in page["items"]]
        pages += 1
        if pages == 1:
            w.case([cs.approval()])  # a new case arrives mid-walk: nothing shifts
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen))
    assert {str(c) for c in created} <= set(seen)
    _error(client.get("/api/v1/cases", params={"cursor": "%%%"}), 422, "validation_failed")
    _error(client.get("/api/v1/cases", params={"limit": 101}), 422, "validation_failed")
    _error(client.get("/api/v1/cases", params={"limit": 0}), 422, "validation_failed")


# ---------------------------------------------------------------- uploads


def _upload(
    client: Client, data: bytes, name: str = "invoice.pdf", mime: str = "application/pdf"
) -> Any:
    return client.mutate("POST", "/api/v1/uploads", files={"file": (name, data, mime)})


def test_upload_creates_an_invoice_and_a_case(web: Web) -> None:
    client = web.client(web.world.analyst)
    response = _upload(client, pdf_bytes(2))
    assert response.status_code == 201, response.text
    body = response.json()
    detail = client.get(f"/api/v1/cases/{body['case_id']}").json()
    assert detail["status"] == "new" and detail["invoice"]["page_count"] == 2
    stored = web.world.conn.execute(
        text("SELECT storage_path, kind::text, source::text FROM documents WHERE id = :d"),
        {"d": body["document_id"]},
    ).one()
    assert stored.kind == "upload" and stored.source == "upload"
    assert (web.store / stored.storage_path).is_file()
    assert stored.storage_path.startswith("uploads/")
    job = web.world.conn.execute(
        text("SELECT type::text FROM jobs WHERE case_id = :c"), {"c": body["case_id"]}
    ).scalar_one()
    assert job == "investigate"
    image = _upload(client, png_bytes(), "scan.png", "image/png")
    assert image.status_code == 201


def test_upload_validation(conn: Connection, tmp_path: Path) -> None:
    web = make_web(conn, tmp_path, max_bytes=50_000)
    client = web.client(web.world.analyst)
    spoofed = _upload(client, b"MZ\x90\x00 not a pdf at all", "invoice.pdf", "application/pdf")
    _error(spoofed, 415, "unsupported_media_type")
    html = _upload(client, b"<html><script>alert(1)</script></html>", "x.png", "image/png")
    _error(html, 415, "unsupported_media_type")
    _error(_upload(client, b"%PDF-1.7\n garbage"), 422, "validation_failed")
    pages = _error(_upload(client, pdf_bytes(21)), 422, "validation_failed")
    assert pages["details"]["reason"] == "too_many_pages"
    _error(_upload(client, b"%PDF-" + b"0" * 60_000), 413, "file_too_large")
    first = pdf_bytes()
    assert _upload(client, first).status_code == 201
    _error(_upload(client, first), 409, "already_uploaded")
    _error(_upload(client, b""), 422, "validation_failed")
    assert not list(tmp_path.glob("uploads/**/.*.part"))


def test_uploads_are_rate_limited_per_user(web: Web) -> None:
    client = web.client(web.world.analyst)
    for _ in range(10):
        _upload(client, b"nope")
    limited = _upload(client, pdf_bytes())
    _error(limited, 429, "rate_limited")
    web.clock.now += 61
    assert _upload(client, pdf_bytes()).status_code == 201


def test_supporting_documents_are_validated_and_reusable(web: Web) -> None:
    case_id, _ = _needs_review(web)
    other, _ = _needs_review(web)
    client = web.client(web.world.analyst)
    letter = pdf_bytes()

    def attach(target: uuid.UUID, kind: str = "credit_note", data: bytes = letter) -> Any:
        return client.mutate(
            "POST",
            f"/api/v1/cases/{target}/supporting-documents",
            files={"file": ("note.pdf", data, "application/pdf")},
            data={"kind": kind, "description": "credit note from the vendor"},
        )

    first = attach(case_id)
    assert first.status_code == 201
    assert attach(other).json()["document_id"] == first.json()["document_id"]  # same file
    _error(attach(case_id, kind="love_letter"), 422, "validation_failed")
    _error(attach(case_id, data=b"GIF89a"), 415, "unsupported_media_type")
    detail = client.get(f"/api/v1/cases/{case_id}").json()
    assert [s["kind"] for s in detail["supporting_documents"]] == ["credit_note"]


# ---------------------------------------------------------------- documents


INVOICE_LINES = (
    ("Invoice A-1 unit price 10.50", 72.0, 700.0),
    ("Pay to account: 2409 7373 2848", 72.0, 400.0),
)
BANK_DIGITS_PX = (340, 2 * (792 - 404))  # inside "2409 7373 2848" at scale 2


def _pixel(png: bytes, at: tuple[int, int]) -> Any:
    with Image.open(io.BytesIO(png)) as image:
        return image.convert("RGB").getpixel(at)


def _downloads(web: Web) -> list[Any]:
    return list(
        web.world.conn.execute(
            text(
                "SELECT actor_id, case_id, payload FROM audit_events"
                " WHERE event_type = 'document.downloaded' ORDER BY seq"
            )
        ).all()
    )


def test_downloads_are_audited_uncached_and_checked_against_their_hash(web: Web) -> None:
    client = web.client(web.world.analyst)
    data = text_pdf(*INVOICE_LINES)
    created = _upload(client, data).json()
    document_id = created["document_id"]
    auditor = web.client(web.world.auditor)
    original = auditor.get(f"/api/v1/documents/{document_id}")
    assert original.status_code == 200
    assert original.content == data  # the evidence, byte for byte
    assert original.headers["content-type"] == "application/pdf"
    assert original.headers["content-disposition"].startswith("attachment;")
    assert original.headers["x-content-type-options"] == "nosniff"
    assert original.headers["cache-control"] == "private, no-store"
    assert original.headers["pragma"] == "no-cache"
    assert "x-opspilot-notice" not in original.headers
    [event] = _downloads(web)
    assert (event.actor_id, str(event.case_id)) == (web.world.auditor, created["case_id"])
    assert event.payload["document_id"] == document_id
    assert auditor.get("/api/v1/me").headers["cache-control"] == "no-store"
    stored = web.world.conn.execute(
        text("SELECT storage_path FROM documents WHERE id = :d"), {"d": document_id}
    ).scalar_one()
    (web.store / stored).write_bytes(data + b"tampered")
    _error(auditor.get(f"/api/v1/documents/{document_id}"), 409, "integrity_failed")
    assert len(_downloads(web)) == 1  # nothing served, nothing recorded
    _error(auditor.get(f"/api/v1/documents/{uuid.uuid4()}"), 404, "not_found")


def test_previews_fail_closed_and_black_out_bank_numbers(web: Web) -> None:
    client = web.client(web.world.analyst)
    document_id = _upload(client, text_pdf(*INVOICE_LINES)).json()["document_id"]
    url = f"/api/v1/documents/{document_id}/pages/1.png"
    _error(client.get(url), 409, "preview_unavailable")  # not parsed yet: no redaction list
    parse_row(web.world.conn, document_id, {"table_cells": [], "items": []})  # missed the account
    page = client.get(url)
    assert page.status_code == 200 and page.content.startswith(b"\x89PNG")
    assert page.headers["cache-control"] == "private, no-store"
    assert _pixel(page.content, BANK_DIGITS_PX) == REDACTION_COLOUR  # pdfium's own detection
    assert _pixel(page.content, (100, 20)) == (255, 255, 255)
    _error(client.get(f"/api/v1/documents/{document_id}/pages/2.png"), 404, "not_found")
    _error(client.get(url, params={"highlight": "x"}), 422, "validation_failed")
    blank = _upload(client, pdf_bytes()).json()["document_id"]  # a scan inside a PDF
    parse_row(web.world.conn, blank, {"table_cells": [], "items": []})
    _error(client.get(f"/api/v1/documents/{blank}/pages/1.png"), 409, "preview_unavailable")


def test_highlights_box_the_cited_values(web: Web) -> None:
    w = web.world
    client = web.client(w.analyst)
    created = _upload(client, text_pdf(*INVOICE_LINES)).json()
    case_id = uuid.UUID(created["case_id"])
    run_id = service.start_investigation(w.conn, case_id)
    check = cs.price()
    check = cs.Check(
        check.code,
        check.status,
        check.compared,
        subject=check.subject,
        refs=[{"entity": "invoice", "id": None, "field": "lines[1].unit_price", "value": "10.50"}],
    )
    w.finish(case_id, run_id, [cs.approval(), check])
    parse_row(
        w.conn,
        created["document_id"],
        {
            "table_cells": [
                {"text": "10.50", "page": 1, "bbox": [230, 712, 262, 698], "origin": "BOTTOMLEFT"}
            ],
            "items": [
                {
                    "text": "Pay to account: BANK_ACCT[fp:3f9a1c2b7d10]",
                    "page": 1,
                    "bbox": [72, 412, 250, 398],
                    "origin": "BOTTOMLEFT",
                }
            ],
        },
    )
    finding = w.finding(case_id, FindingCode.PRICE_MISMATCH)
    url = f"/api/v1/documents/{created['document_id']}/pages/1.png"
    plain = client.get(url).content
    boxed = client.get(url, params={"highlight": f"finding:{finding}"})
    assert boxed.status_code == 200 and boxed.content != plain
    assert _pixel(plain, (2 * 100, 2 * (792 - 405))) == REDACTION_COLOUR  # the parse's token
    _error(client.get(url, params={"highlight": f"finding:{uuid.uuid4()}"}), 404, "not_found")


def test_image_uploads_cannot_bypass_the_privacy_controls(web: Web) -> None:
    client = web.client(web.world.analyst)
    created = _upload(client, png_bytes(), "scan.png", "image/png")
    assert created.status_code == 201
    body = created.json()
    assert body["redaction_supported"] is False and "OCR" in body["notice"]
    detail = client.get(f"/api/v1/cases/{body['case_id']}").json()
    assert detail["invoice"]["redaction_supported"] is False
    assert detail["invoice"]["notice"] == body["notice"]
    url = f"/api/v1/documents/{body['document_id']}/pages/1.png"
    _error(client.get(url), 409, "preview_unavailable")
    parse_row(  # even with coordinates that would allow a redaction list
        web.world.conn,
        body["document_id"],
        {"items": [{"text": "x", "page": 1, "bbox": [0, 0, 1, 1], "origin": "TOPLEFT"}]},
    )
    _error(client.get(url), 409, "preview_unavailable")
    page = client.get(f"/cases/{body['case_id']}")
    assert "Not redacted." in page.text and "/pages/1.png" not in page.text
    original = web.client(web.world.auditor).get(f"/api/v1/documents/{body['document_id']}")
    assert original.status_code == 200
    assert original.headers["x-opspilot-notice"].startswith("unredacted-scan")
    assert original.headers["cache-control"] == "private, no-store"
    assert len(_downloads(web)) == 1  # authorised review leaves a trace


# ---------------------------------------------------------------- evaluation cases


def test_evaluation_cases_are_invisible(web: Web) -> None:
    w = web.world
    w.conn.execute(text("RESET ROLE"))
    eval_run = w.conn.execute(
        text(
            "INSERT INTO eval_runs (system, dataset_id, provider, model, prompt_versions,"
            " policy_version_id, git_sha, status) VALUES ('opspilot', :d, 'fake', 'fake-1',"
            " '{}'::jsonb, :p, 'abc', 'running') RETURNING id"
        ),
        {"d": w.dataset, "p": w.policy_version},
    ).scalar_one()
    invoice = f.invoice(w.conn, w.dataset)
    case_id = service.create_eval_case(w.conn, eval_run, invoice)
    run_id = service.start_investigation(w.conn, case_id)
    w.finish(case_id, run_id, [cs.approval(), cs.price()])
    document = w.conn.execute(
        text("SELECT document_id FROM invoices WHERE id = :i"), {"i": invoice}
    ).scalar_one()
    w.conn.execute(text("SET LOCAL ROLE opspilot_app"))
    for user in (w.analyst, w.approver, w.controller, w.auditor):
        client = web.client(user)
        base = f"/api/v1/cases/{case_id}"
        _error(client.get(base), 404, "not_found")
        _error(client.get(f"{base}/audit"), 404, "not_found")
        _error(client.get(f"{base}/runs/{run_id}/trace"), 404, "not_found")
        _error(client.get(f"/api/v1/documents/{document}"), 404, "not_found")
        _error(client.get(f"/api/v1/documents/{document}/pages/1.png"), 404, "not_found")
        for path in ("dismiss", "approve", "decline", "submit", "recheck"):
            body: dict[str, Any] = {"expected_version": 1}
            if path in {"dismiss", "decline", "recheck"}:
                body["reason"] = "x"
            _error(client.mutate("POST", f"{base}/{path}", json=body), 404, "not_found")
        assert str(case_id) not in client.get("/api/v1/cases", params={"limit": 100}).text
    approver = web.client(w.controller)
    assert str(case_id) not in approver.get("/api/v1/approvals").text


# ---------------------------------------------------------------- secrets and bank data


def test_no_bank_numbers_or_secrets_in_any_response_or_log(
    web: Web, capsys: pytest.CaptureFixture[str]
) -> None:
    w = web.world
    case_id = w.case([cs.approval(), cs.bank_change()])
    analyst = web.client(w.analyst)
    letter = analyst.mutate(
        "POST",
        f"/api/v1/cases/{case_id}/supporting-documents",
        files={"file": ("letter.pdf", pdf_bytes(), "application/pdf")},
        data={"kind": SupportingKind.BANK_VERIFICATION.value, "description": "signed letter"},
    ).json()["supporting_document_id"]
    recorded = analyst.mutate(
        "POST",
        f"/api/v1/cases/{case_id}/bank-verifications",
        json={
            "channel": "callback_to_master_contact",
            "contact_source": "vendor_master",
            "contact_reference": "phoned the number on the vendor master",
            "verified_account_number": cs.CHANGED_ACCOUNT,
            "evidence_document_id": letter,
            "expected_version": analyst.version(case_id),
        },
    )
    assert recorded.status_code == 201, recorded.text
    assert recorded.json()["account"] == "••••3459"
    detail = analyst.get(f"/api/v1/cases/{case_id}").json()
    run_id = detail["evidence_run"]["id"]
    analyst.get(f"/api/v1/cases/{case_id}/audit")
    analyst.get(f"/api/v1/cases/{case_id}/runs/{run_id}/trace")
    analyst.get("/api/v1/cases")
    analyst.get(f"/cases/{case_id}")
    analyst.mutate("POST", "/auth/logout")

    fingerprints = {
        fingerprint(cs.CHANGED_ACCOUNT, cs.BANK_KEY).bank_fp,
        fingerprint(cs.MASTER_ACCOUNT, cs.BANK_KEY).bank_fp,
    }
    digits = {
        "".join(ch for ch in a if ch.isdigit()) for a in (cs.CHANGED_ACCOUNT, cs.MASTER_ACCOUNT)
    }
    secrets = {
        web.app().state.settings.session_secret.get_secret_value(),
        cs.BANK_KEY,
        PASSWORD,
    }
    forbidden = {cs.CHANGED_ACCOUNT, cs.MASTER_ACCOUNT, *digits, *fingerprints, *secrets}
    for response in web.responses:
        body = response.text if "image" not in response.headers.get("content-type", "") else ""
        for value in forbidden:
            assert value not in body, (response.request.url, value)
        assert response.headers.get("set-cookie", "").count(PASSWORD) == 0
    output = capsys.readouterr()
    logs = output.out + output.err
    for value in forbidden | {analyst.csrf}:
        assert value not in logs
    banks = web.world.conn.execute(text("SELECT * FROM bank_verifications")).mappings().all()
    assert all(cs.CHANGED_ACCOUNT not in json.dumps(dict(b), default=str) for b in banks)


# ---------------------------------------------------------------- HTML twins


def test_html_forms_use_the_same_rules(web: Web) -> None:
    case_id, _ = _needs_review(web)
    browser = web.client()
    page = browser.get("/login")
    assert page.status_code == 200 and "Sign in" in page.text
    signed = browser.post(
        "/auth/login",
        data={"username": web.username(web.world.analyst), "password": PASSWORD},
        headers={"Accept": "text/html"},
    )
    assert signed.status_code == 303 and signed.headers["location"] == "/"
    queue = browser.get("/")
    assert queue.status_code == 200 and str(case_id) in queue.text
    case_page = browser.get(f"/cases/{case_id}")
    assert case_page.status_code == 200
    token = case_page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    version = browser.version(case_id)
    no_token = browser.post(
        f"/cases/{case_id}/dismiss", data={"reason": "dup", "expected_version": str(version)}
    )
    # A failed form goes back to its page with the code; the page words it inline.
    assert no_token.status_code == 303
    assert no_token.headers["location"] == f"/cases/{case_id}?error=csrf_failed"
    shown = browser.get(no_token.headers["location"])
    assert shown.status_code == 200 and "The form expired. Try again." in shown.text
    stale = browser.post(
        f"/cases/{case_id}/dismiss",
        data={"reason": "dup", "expected_version": "99", "csrf_token": token},
    )
    assert stale.headers["location"] == f"/cases/{case_id}?error=stale_version"
    spoofed = browser.get(f"/cases/{case_id}?error=<script>alert(1)</script>")
    assert "<script>alert(1)" not in spoofed.text and "That couldn&#39;t be done." in spoofed.text
    done = browser.post(
        f"/cases/{case_id}/proposal",
        data={
            "action": "draft_vendor_query",
            "reason": "ask the vendor",
            "expected_version": str(version),
            "csrf_token": token,
        },
    )
    assert done.status_code == 303 and done.headers["location"] == f"/cases/{case_id}"
    assert browser.version(case_id) == version + 1
    assert browser.get("/uploads/new").status_code == 200
    auditor = web.client()
    auditor.post(
        "/auth/login",
        data={"username": web.username(web.world.auditor), "password": PASSWORD},
        headers={"Accept": "text/html"},
    )
    assert auditor.get("/uploads/new").status_code == 403
    assert auditor.get("/approvals").status_code == 403
    approver = web.client()
    approver.post(
        "/auth/login",
        data={"username": web.username(web.world.approver), "password": PASSWORD},
        headers={"Accept": "text/html"},
    )
    for path in ("/approvals", "/approvals?status=held_for_approver", "/", f"/cases/{case_id}"):
        assert approver.get(path).status_code == 200, path  # the approver's nav renders
    out = browser.post("/auth/logout", data={"csrf_token": token}, headers={"Accept": "text/html"})
    assert out.status_code == 303 and out.headers["location"] == "/login"


def test_a_duplicate_can_be_resolved_over_http_with_both_numbers(web: Web) -> None:
    w = web.world
    ledger = w.ledger()
    case_id = w.case([cs.approval(), cs.duplicate(ledger)])
    finding = w.finding(case_id, FindingCode.DUPLICATE_INVOICE)
    client = web.client(w.analyst)
    confirmation = client.mutate(
        "POST",
        f"/api/v1/cases/{case_id}/supporting-documents",
        files={"file": ("confirmation.pdf", pdf_bytes(), "application/pdf")},
        data={"kind": "vendor_confirmation", "description": "vendor confirms both invoices"},
    ).json()["supporting_document_id"]
    resolved = client.mutate(
        "POST",
        f"/api/v1/cases/{case_id}/findings/{finding}/resolutions",
        json={
            "resolution_type": "not_duplicate",
            "summary": "different shipment",
            "evidence": [
                {"supporting_document_id": confirmation},
                {"erp_entity": "ledger_invoices", "erp_id": str(ledger)},
            ],
            "structured": {
                "both_invoice_numbers": [cs.INVOICE_NUMBER, cs.LEDGER_NUMBER],
                "distinguishing_fact": "different_goods",
            },
            "expected_version": client.version(case_id),
        },
    )
    assert resolved.status_code == 201, resolved.text
    detail = client.get(f"/api/v1/cases/{case_id}").json()
    duplicate = next(f for f in detail["findings"] if f["exception_type"] == "DUPLICATE_INVOICE")
    assert duplicate["resolved"] is True
    assert duplicate["resolution"]["digest"] == resolved.json()["digest"]
