"""The 3D scenes' data and 2D fallbacks (`07-design.md` §11, P13), without a browser."""

import datetime as dt
import gzip
import itertools
import json
import re
from pathlib import Path
from typing import Any

from opspilot.web.app import templates
from opspilot.web.scenes import (
    MIN_GAP,
    audit_chain,
    desk_approvals,
    desk_queue,
    document_fan,
    hero,
    match_stack,
    pipeline_live,
    pipeline_track,
    resolution_chain,
)

STATIC = Path(__file__).parents[2] / "src/opspilot/web/static"
T0 = dt.datetime(2026, 10, 27, 9, 0, tzinfo=dt.UTC)


def render(scene: Any) -> str:
    template = templates.env.from_string(
        '{% from "partials/scene.html" import scene %}{{ scene(s) }}'
    )
    return template.render(s=scene)


def price_case() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    strip = [
        {
            "line": 1,
            "description": "Hex bolts",
            "quantity": "120",
            "unit_price": "0.45",
            "line_total": "54.00",
            "po": {
                "line_no": 1,
                "description": "Hex bolts",
                "quantity": "120",
                "unit_price": "0.42",
            },
            "received": "120",
            "method": "sku",
            "similarity": None,
            "breaks": [{"check_code": "price", "status": "fail"}],
        },
        {
            "line": 2,
            "description": "Washers",
            "quantity": "10",
            "unit_price": "1.00",
            "line_total": "10.00",
            "po": None,
            "received": None,
            "method": "unmatched",
            "similarity": None,
            "breaks": [],
        },
    ]
    checks = [
        {
            "id": "c-price",
            "check_code": "price",
            "subject_key": "line:1",
            "status": "fail",
            "compared": {
                "invoice_unit_price": "0.45",
                "po_unit_price": "0.42",
                "variance_pct": "7.14",
            },
            "tolerance": {"max_overcharge_pct": "2"},
        }
    ]
    findings = [{"id": "f-1", "check_result_ids": ["c-price"]}]
    return strip, checks, findings


def test_the_match_stack_breaks_between_the_planes_that_disagree() -> None:
    scene = match_stack(*price_case())
    first, second = scene.payload["lines"]
    assert first["breaks"] == [
        {"from": "po", "to": "invoice", "status": "fail", "text": "Price +7.14%"}
    ]
    assert first["values"] == {"po": "120 × $0.42", "receipt": "120", "invoice": "120 × $0.45"}
    assert (first["row"], first["finding"]) == ("strip-line-1", "finding-f-1")
    assert second["values"]["po"] == "—" and second["breaks"] == []
    assert "line 1 Price +7.14%" in scene.label
    # The fallback draws the same break with the same words, and links to the same row.
    html = render(scene)
    assert 'href="#strip-line-1"' in html and "Price +7.14%" in html and "sv-break fail" in html


def test_the_payload_and_its_fallback_carry_the_same_data() -> None:
    scene = match_stack(*price_case())
    html = render(scene)
    payload = json.loads(re.search(r"data-scene-payload>(.*?)</script>", html, re.S).group(1))  # type: ignore[union-attr]
    assert payload == {"label": scene.label, **scene.payload}
    assert f'aria-label="{scene.label}"' in html


def test_text_from_documents_is_escaped_in_the_fallback_and_the_payload() -> None:
    strip, checks, findings = price_case()
    strip[1]["description"] = '</script><img src=x onerror="alert(1)">'
    html = render(match_stack(strip, checks, findings))
    assert "<img" not in html and "</script><" not in html
    fan = document_fan(
        {"page_count": 1},
        [{"id": "s1", "kind": "credit_note", "description": "<b>CN</b>"}],
        page=1,
        highlight=None,
        previews=True,
    )
    assert "<b>CN</b>" not in render(fan)


def test_desk_piles_share_one_scale_and_ink_marks_the_viewers_work() -> None:
    scene = desk_queue({"needs_review": 4, "closed": 1, "pending_approval": 2}, "analyst", "closed")
    assert scene is not None
    piles = {p["id"]: p for p in scene.payload["piles"]}
    assert [p["id"] for p in scene.payload["piles"]] == [
        "pile-needs_review",
        "pile-pending_approval",
        "pile-closed",
    ]
    assert piles["pile-needs_review"]["ink"] and not piles["pile-closed"]["ink"]
    assert (piles["pile-needs_review"]["share"], piles["pile-closed"]["share"]) == (1.0, 0.25)
    assert piles["pile-closed"]["current"] and piles["pile-closed"]["href"] == "/?status=closed"
    html = render(scene)
    assert 'href="/?status=closed" aria-current="true"' in html and 'width="25.0"' in html
    assert desk_queue({}, "analyst", None) is None  # no work, no scene


def test_approvals_desk_has_no_link_to_a_list_the_approver_cannot_see() -> None:
    scene = desk_approvals({"mine": 2, "others": 1, "held": 0}, unlimited=False, held=False)
    assert scene is not None
    others = scene.payload["piles"][1]
    assert others["label"] == "Needs a controller" and others["href"] is None
    assert [p["id"] for p in scene.payload["piles"]] == ["pile-mine", "pile-others"]


def test_resolution_links_point_at_the_html_checkboxes() -> None:
    finding = {
        "exception_type": "PRICE_MISMATCH",
        "resolution": {
            "id": "r1",
            "resolution_type": "credit_issued",
            "evidence": [{"supporting_document_id": "s1"}, {"erp_entity": "ledger_invoices"}],
        },
    }
    scene = resolution_chain(
        [finding, {"exception_type": "TAX_MISCALCULATED"}], [{"id": "s1", "kind": "credit_note"}]
    )
    (item,) = scene.payload["items"]
    assert item == {
        "id": "chain-r1",
        "finding": "Price above PO",
        "resolution": "Credit issued",
        "evidence": ["Credit note", "ERP ledger invoices"],
        "accept": "accept-r1",
    }
    html = render(scene)
    assert 'data-accept-link="accept-r1"' in html and 'href="#accept-r1"' in html


def test_pipeline_stations_follow_the_records_in_order_with_waits_and_calls() -> None:
    trace = {
        "run": {
            "started_at": T0,
            "finished_at": T0 + dt.timedelta(seconds=40),
            "status": "succeeded",
        },
        "llm_calls": [
            {
                "purpose": "investigate",
                "created_at": T0 + dt.timedelta(seconds=20),
                "latency_ms": 2000,
                "quota_wait_ms": 8000,
            },
            {
                "purpose": "explain",
                "created_at": T0 + dt.timedelta(seconds=30),
                "latency_ms": 1000,
                "quota_wait_ms": 0,
            },
        ],
    }
    timeline: dict[str, dt.datetime | None] = {
        "extract": T0 + dt.timedelta(seconds=4),
        "run_checks": T0 + dt.timedelta(seconds=6),
        "persist_findings": T0 + dt.timedelta(seconds=32),
    }
    scene = pipeline_track(trace, timeline)
    stations = {s["key"]: s for s in scene.payload["stations"]}
    positions = [s["p"] for s in scene.payload["stations"]]
    assert positions == sorted(positions) and positions[0] == 0.0 and positions[-1] == 1.0
    assert all(b - a >= MIN_GAP / 2 for a, b in itertools.pairwise(positions))
    assert stations["investigate"]["at"] == 20.0 and stations["validate"]["at"] is None
    assert all(s["done"] for s in stations.values())
    (wait,) = scene.payload["waits"]
    assert wait["seconds"] == 8.0 and wait["from"] < wait["to"] <= stations["investigate"]["p"]
    assert len(scene.payload["markers"]) == 2
    assert "8.0 s waiting for AI quota" in scene.label
    assert 'href="#trace-step-validate"' in render(scene)


def test_a_run_written_in_one_moment_still_reads_in_order() -> None:
    trace = {"run": {"started_at": T0, "finished_at": T0, "status": "succeeded"}, "llm_calls": []}
    moment: dict[str, dt.datetime | None] = {
        "extract": T0,
        "run_checks": T0,
        "persist_findings": T0,
    }
    scene = pipeline_track(trace, moment)
    positions = [s["p"] for s in scene.payload["stations"]]
    assert positions == sorted(positions) and len(set(positions)) == len(positions)


def test_the_live_track_lights_the_current_step_and_shows_a_quota_wait() -> None:
    progress = {
        "steps": [
            {"key": "records", "label": "Gathering records", "done": True, "current": False},
            {"key": "extract", "label": "Reading document", "done": False, "current": True},
            {"key": "match", "label": "Matching lines", "done": False, "current": False},
        ],
        "quota_resumes_at": T0,
    }
    scene = pipeline_live(progress)
    assert [s["current"] for s in scene.payload["stations"]] == [False, True, False]
    assert scene.payload["waits"] and "waiting for AI quota" in scene.label
    assert all(s["target"] is None for s in scene.payload["stations"])


def test_the_audit_chain_shows_verified_unchecked_and_broken_links() -> None:
    items = [
        {"seq": n, "event_type": t, "hash": f"{n:02d}" * 32, "payload": p}
        for n, t, p in [
            (10, "case.transition", {}),
            (11, "run.completed", {"evidence_digest": "ab" * 32}),
            (12, "case.transition", {}),
            (13, "case.transition", {}),
        ]
    ]
    verification = {"verified_through_seq": 12, "broken_seq": None}
    chain = audit_chain({"items": items, "verification": verification})
    assert chain is not None
    blocks = chain.payload["blocks"]
    assert [b["state"] for b in blocks] == ["verified", "verified", "verified", "unchecked"]
    assert blocks[1]["seal"] == "abababab" and blocks[0]["hash"] == "10101010"
    broken = audit_chain(
        {"items": items, "verification": {"verified_through_seq": 13, "broken_seq": 12}}
    )
    assert broken is not None
    assert [b["state"] for b in broken.payload["blocks"]] == [
        "verified",
        "verified",
        "broken",
        "unchecked",
    ]
    assert "broken at event 12" in broken.label
    never = audit_chain({"items": items, "verification": None})
    assert never is not None
    assert {b["state"] for b in never.payload["blocks"]} == {"unchecked"}
    assert audit_chain({"items": [], "verification": None}) is None


def test_the_document_fan_opens_pages_but_never_downloads() -> None:
    scene = document_fan(
        {"page_count": 2},
        [{"id": "s1", "kind": "credit_note", "description": "CN-3318"}],
        page=2,
        highlight="f-1",
        previews=True,
    )
    invoice, credit = scene.payload["documents"]
    assert [p["href"] for p in invoice["pages"]] == [
        "?page=1&highlight=f-1#documents",
        "?page=2&highlight=f-1#documents",
    ]
    assert [p["forward"] for p in invoice["pages"]] == [False, True]
    assert credit["pages"][0]["href"] is None and credit["pages"][0]["target"] == "doc-s1"
    assert "/api/v1/documents" not in render(scene)
    scan = document_fan({"page_count": 1}, [], page=1, highlight=None, previews=False)
    assert scan.payload["documents"][0]["pages"][0]["target"] == "doc-invoice"


def test_the_sign_in_hero_is_decorative_and_reads_no_case() -> None:
    scene = hero()
    html = render(scene)
    assert scene.decorative and scene.payload["idle"] == 6
    assert 'aria-hidden="true"' in html and "<a " not in html


def test_every_scene_module_exists_and_the_bundle_fits_its_budget() -> None:
    """three.js + stage + all scenes ≤ 260 KB gzipped; one scene adds ≤ 20 KB (§11.4)."""

    def gz(path: Path) -> int:
        return len(gzip.compress(path.read_bytes(), compresslevel=9))

    scenes = sorted((STATIC / "js/scenes").glob("*.js"))
    assert {p.stem for p in scenes} == {
        "match_stack",
        "desk",
        "resolution_chain",
        "pipeline",
        "audit_chain",
        "document_fan",
    }
    total = gz(STATIC / "vendor/three/three.module.min.js") + gz(STATIC / "js/stage3d.js")
    total += sum(gz(p) for p in scenes)
    assert total <= 260 * 1024, total
    assert all(gz(p) <= 20 * 1024 for p in scenes)


def test_scene_code_never_talks_to_the_network_or_writes_html() -> None:
    """3D can't trigger a mutation: no fetch, XHR, form or innerHTML in any scene code."""
    for path in [STATIC / "js/stage3d.js", *(STATIC / "js/scenes").glob("*.js")]:
        code = path.read_text()
        for banned in (
            "fetch(",
            "XMLHttpRequest",
            "sendBeacon",
            ".submit(",
            "innerHTML",
            "insertAdjacentHTML",
            "<form",
        ):
            assert banned not in code, (path.name, banned)
