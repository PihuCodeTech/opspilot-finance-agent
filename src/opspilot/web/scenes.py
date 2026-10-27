"""The 3D scenes' data (`07-design.md` §11): one payload per scene, read by the
three.js view and drawn server-side as the 2D fallback, so the two can't disagree.

Pure display, like `present.py`: nothing here decides anything. Each builder returns
a `Scene` with the JSON payload the browser reads and the geometry its SVG fallback
draws. Every link in a payload points at HTML on the same page or at a GET page;
nothing a scene can do submits, accepts or approves.
"""

import datetime as dt
import itertools
import math
from dataclasses import dataclass, field
from typing import Any

from opspilot.web.present import (
    CHECK,
    DASH,
    FINDING,
    RESOLUTION,
    STATUS,
    SUPPORTING_KIND,
    evidence_rows,
    money,
    quantity,
)


@dataclass(frozen=True)
class Scene:
    kind: str  # the scene module and the fallback template
    id: str  # unique on the page
    label: str  # the canvas's accessible summary, built from the data
    payload: dict[str, Any]
    svg: dict[str, Any] = field(default_factory=dict)
    decorative: bool = False  # the sign-in hero: aria-hidden, no links

    def data(self) -> dict[str, Any]:
        return {"label": self.label, **self.payload}


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _short(text: str, size: int) -> str:
    """Fits a fallback box; the full text stays in the link's accessible name."""
    return text if len(text) <= size else text[: size - 1].rstrip() + "…"


# ---------------------------------------------------------------- 1, 7: the match stack


PLANES = (("po", "PO"), ("receipt", "Received"), ("invoice", "Invoice"))
# Which planes disagree when a line breaks a check; arithmetic is the invoice against itself.
BREAK_PLANES = {
    "price": ("po", "invoice"),
    "quantity": ("receipt", "invoice"),
    "arithmetic": ("invoice", "invoice"),
}
# Isometric layout of the fallback (viewBox units): plane length, plane depth, plane spacing.
U, V, PLANE_GAP = (380.0, -95.0), (150.0, 45.0), 72.0


def _break_text(check: dict[str, Any] | None, code: str) -> str:
    if check is None:
        return CHECK.get(code, code)
    if check.get("status") == "inconclusive":
        return f"{CHECK.get(code, code)} inconclusive"
    rows = evidence_rows(check)
    difference = rows[0].difference if rows else DASH
    return f"{CHECK.get(code, code)} {difference}" if difference != DASH else CHECK.get(code, code)


def match_stack(
    strip: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    *,
    scene_id: str = "match-stack",
) -> Scene:
    """PO, receipt and invoice as three sheets, each line threaded through all three."""
    by_subject = {(c["check_code"], c.get("subject_key")): c for c in checks}
    finding_for = {str(cid): f["id"] for f in findings for cid in f.get("check_result_ids", [])}
    lines = []
    for row in strip:
        n = row["line"]
        po = row.get("po")
        breaks = []
        finding = None
        for b in row.get("breaks", []):
            code = str(b["check_code"])
            check = by_subject.get((code, f"line:{n}"))
            low, high = BREAK_PLANES.get(code, ("po", "invoice"))
            breaks.append(
                {"from": low, "to": high, "status": b["status"], "text": _break_text(check, code)}
            )
            if check is not None and finding is None:
                finding = finding_for.get(str(check["id"]))
        lines.append(
            {
                "id": f"line-{n}",
                "line": n,
                "description": row.get("description") or DASH,
                "values": {
                    "po": f"{quantity(po['quantity'])} × {money(po['unit_price'])}" if po else DASH,
                    "receipt": quantity(row.get("received")),
                    "invoice": f"{quantity(row.get('quantity'))} × {money(row.get('unit_price'))}",
                },
                "breaks": breaks,
                "row": f"strip-line-{n}",
                "finding": f"finding-{finding}" if finding else None,
            }
        )
    broken = [f"line {x['line']} {b['text']}" for x in lines for b in x["breaks"]]
    label = (
        f"Match stack: {_plural(len(lines), 'invoice line')} threaded through the PO,"
        " what was received and the invoice. "
        + (f"Breaks: {'; '.join(broken)}." if broken else "Every line matches.")
    )
    payload = {"planes": [{"key": k, "label": t} for k, t in PLANES], "lines": lines}
    return Scene("match_stack", scene_id, label, payload, _match_stack_svg(lines))


def _point(plane: int, t: float) -> tuple[float, float]:
    """A point on plane 0 (PO, bottom) to 2 (invoice, top), at t along its length."""
    x0, y0 = 40.0, 70.0 + 2 * PLANE_GAP + 95.0 - plane * PLANE_GAP
    return (x0 + t * U[0] + 0.5 * V[0], y0 + t * U[1] + 0.5 * V[1])


def _match_stack_svg(lines: list[dict[str, Any]]) -> dict[str, Any]:
    index = {key: i for i, (key, _) in enumerate(PLANES)}
    planes = []
    for i, (_, label) in enumerate(PLANES):
        x0, y0 = 40.0, 70.0 + 2 * PLANE_GAP + 95.0 - i * PLANE_GAP
        corners = [(x0, y0), (x0 + U[0], y0 + U[1]), (x0 + U[0] + V[0], y0 + U[1] + V[1])]
        corners.append((x0 + V[0], y0 + V[1]))
        planes.append(
            {
                "points": " ".join(f"{x:.1f},{y:.1f}" for x, y in corners),
                "label": label,
                "x": round(x0 + U[0] + V[0] + 10, 1),
                "y": round(y0 + U[1] + 0.5 * V[1] + 4, 1),
                "front": i == len(PLANES) - 1,
            }
        )
    threads = []
    count = max(len(lines), 1)
    for k, line in enumerate(lines):
        t = (k + 0.5) / count
        points = [_point(i, t) for i in range(len(PLANES))]
        breaks = []
        for b in line["breaks"]:
            low, high = index[b["from"]], index[b["to"]]
            a, z = points[low], points[high]
            breaks.append(
                {
                    "self": low == high,
                    "x1": round(a[0], 1),
                    "y1": round(a[1], 1),
                    "x2": round(z[0], 1),
                    "y2": round(z[1], 1),
                    "status": b["status"],
                    "text": b["text"],
                    "tx": round(z[0] + 8, 1),
                    "ty": round((a[1] + z[1]) / 2 + 4, 1),
                }
            )
        top = points[-1]
        threads.append(
            {
                "line": line["line"],
                "href": f"#{line['row']}",
                "aria": f"Line {line['line']}: "
                + ("; ".join(b["text"] for b in line["breaks"]) or "matches"),
                "path": " ".join(f"{x:.1f},{y:.1f}" for x, y in points),
                "x": round(top[0], 1),
                "y": round(top[1] - 10, 1),
                "breaks": breaks,
            }
        )
    return {"w": 640, "h": 400, "planes": planes, "threads": threads}


# A stack for the sign-in page: illustrative, not read from any case (no data before sign-in).
HERO_STRIP: list[dict[str, Any]] = [
    {
        "line": n,
        "description": description,
        "quantity": qty,
        "unit_price": price,
        "po": {"quantity": qty, "unit_price": po_price},
        "received": received,
        "breaks": breaks,
    }
    for n, (description, qty, price, po_price, received, breaks) in enumerate(
        [
            ("Hex bolts", "400", "0.18", "0.18", "400", []),
            ("Bearings", "24", "13.10", "12.40", "24", [{"check_code": "price", "status": "fail"}]),
            ("Gaskets", "60", "2.75", "2.75", "60", []),
            ("Seals", "36", "4.20", "4.20", "36", []),
        ],
        start=1,
    )
]


def hero() -> Scene:
    scene = match_stack(HERO_STRIP, [], [], scene_id="hero")
    return Scene(
        "match_stack",
        "hero",
        "",
        {**scene.payload, "idle": 6},
        scene.svg,
        decorative=True,
    )


# ---------------------------------------------------------------- 2: the desk overview


DESK_ORDER = (
    "needs_review",
    "investigation_failed",
    "new",
    "investigating",
    "pending_approval",
    "held_for_approver",
    "approved",
    "closed",
)
NEEDS_ME = {
    "analyst": {"needs_review", "investigation_failed"},
    "approver": {"pending_approval"},
}


def _desk(piles: list[dict[str, Any]], scope: str, scene_id: str) -> Scene | None:
    if not piles:
        return None
    top = max(p["count"] for p in piles)
    for p in piles:
        # Height and bar length both follow the count on one linear scale.
        p["share"] = round(p["count"] / top, 4) if top else 0
        p["width"] = round(100 * p["share"], 2)
    label = f"{scope}: " + ", ".join(f"{p['count']} {p['label'].lower()}" for p in piles) + "."
    return Scene("desk", scene_id, label, {"piles": piles}, {"piles": piles})


def desk_queue(counts: dict[str, int], role: str, current: str | None) -> Scene | None:
    """Work as piles, one per status; the piles that need the viewer are in ink."""
    mine = NEEDS_ME.get(role, set())
    piles = [
        {
            "id": f"pile-{status}",
            "label": STATUS[status][2],
            "count": counts[status],
            "ink": status in mine,
            "href": f"/?status={status}",
            "current": status == current,
        }
        for status in DESK_ORDER
        if counts.get(status)
    ]
    return _desk(piles, "Cases by status", "desk")


def desk_approvals(counts: dict[str, Any], unlimited: bool, held: bool) -> Scene | None:
    """What waits for this approver, what needs someone else, and what is held.

    The approvals list only holds cases this approver may approve, so the middle pile
    has no list to filter to (`07-design.md` §11.2 asks for one pile per tier [A]).
    """
    piles = [
        {
            "id": "pile-mine",
            "label": "Within your " + ("authority" if unlimited else "limit"),
            "count": counts.get("mine", 0),
            "ink": True,
            "href": "/approvals",
            "current": not held,
        },
        {
            "id": "pile-others",
            "label": "Needs " + ("another approver" if unlimited else "a controller"),
            "count": counts.get("others", 0),
            "ink": False,
            "href": None,
            "current": False,
        },
        {
            "id": "pile-held",
            "label": "Held: no approver",
            "count": counts.get("held", 0),
            "ink": False,
            "href": "/approvals?status=held_for_approver",
            "current": held,
        },
    ]
    return _desk([p for p in piles if p["count"]], "Approvals", "desk")


# ---------------------------------------------------------------- 3: the resolution chain


def _evidence_label(entry: dict[str, Any], supporting: dict[str, dict[str, Any]]) -> str:
    if entry.get("supporting_document_id"):
        doc = supporting.get(str(entry["supporting_document_id"]))
        return SUPPORTING_KIND.get(doc["kind"], doc["kind"]) if doc else "Supporting document"
    if entry.get("bank_verification_id"):
        return "Bank verification record"
    return f"ERP {entry.get('erp_entity', 'record')}".replace("_", " ")


def resolution_chain(
    findings: list[dict[str, Any]], supporting_docs: list[dict[str, Any]]
) -> Scene:
    """Each finding linked back to its resolution and that resolution's evidence."""
    supporting = {str(s["id"]): s for s in supporting_docs}
    items = [
        {
            "id": f"chain-{f['resolution']['id']}",
            "finding": FINDING.get(f["exception_type"], f["exception_type"]),
            "resolution": RESOLUTION.get(
                f["resolution"]["resolution_type"], f["resolution"]["resolution_type"]
            ),
            "evidence": [_evidence_label(e, supporting) for e in f["resolution"]["evidence"]],
            "accept": f"accept-{f['resolution']['id']}",
        }
        for f in findings
        if f.get("resolution")
    ]
    label = (
        f"Resolution chain: {_plural(len(items), 'finding')}, each linked to its resolution"
        " and evidence. "
        + "; ".join(f"{i['finding']}: {i['resolution']}, {', '.join(i['evidence'])}" for i in items)
        + ". Links turn solid as each resolution is accepted."
    )
    return Scene("resolution_chain", "resolution-chain", label, {"items": items}, _chain_svg(items))


def _chain_svg(items: list[dict[str, Any]]) -> dict[str, Any]:
    rows, y = [], 24.0
    for item in items:
        evidence = item["evidence"] or [DASH]
        height = max(2, len(evidence)) * 36.0
        mid = y + height / 2
        rows.append(
            {
                **item,
                "finding_short": _short(item["finding"], 24),
                "resolution_short": _short(item["resolution"], 24),
                "y": round(mid - 14, 1),
                "mid": round(mid, 1),
                "evidence_rows": [
                    {
                        "text": _short(text, 24),
                        "y": round(y + 36 * i + 4, 1),
                        "mid": round(y + 36 * i + 18, 1),
                    }
                    for i, text in enumerate(evidence)
                ],
            }
        )
        y += height + 12
    return {"w": 640, "h": round(max(y + 12, 120.0), 1), "rows": rows}


# ---------------------------------------------------------------- 4: the pipeline track


STATIONS = (
    ("load_case", "Load"),
    ("extract", "Extract"),
    ("resolve_records", "Resolve records"),
    ("match_lines", "Match"),
    ("run_checks", "Checks"),
    ("investigate", "Investigate"),
    ("explain_and_propose", "Explain"),
    ("validate", "Validate"),
    ("persist_findings", "Persist"),
    ("await_decision", "Await decision"),
)
LLM_STATION = {"investigate": "investigate", "explain": "explain_and_propose"}
MIN_GAP = 0.05  # stations written in the same moment still read in order


def _seconds(when: dt.datetime | None, start: dt.datetime) -> float | None:
    return None if when is None else max(0.0, (when - start).total_seconds())


def _ends(calls: list[dict[str, Any]], start: dt.datetime) -> dict[str, float]:
    """When the last call for each LLM-backed station ended (calls are recorded on return)."""
    ends: dict[str, float] = {}
    for c in calls:
        station = LLM_STATION.get(str(c.get("purpose")))
        at = _seconds(c.get("created_at"), start)
        if station and at is not None:
            ends[station] = max(ends.get(station, 0.0), at)
    return ends


def _scale(knots: list[tuple[float, float]], t: float) -> float:
    """Time to track position through the stations' (time, position) pairs."""
    if not knots:
        return 0.0
    for (t0, p0), (t1, p1) in itertools.pairwise(knots):
        if t <= t1:
            return p0 if t1 <= t0 else p0 + (p1 - p0) * (t - t0) / (t1 - t0)
    return knots[-1][1]


def _spread(raw: list[float | None]) -> list[float]:
    """Track positions: timed stations by time, untimed ones evenly between their timed
    neighbours, nudged apart so stations written in the same moment keep their order."""
    known = [(i, p) for i, p in enumerate(raw) if p is not None]
    if not known:
        return [i / (len(raw) - 1) for i in range(len(raw))]
    anchors = [(-1, 0.0), *known, (len(raw), 1.0)]
    filled: list[float] = []
    for i, given in enumerate(raw):
        if given is None:
            j0, p0 = max((a for a in anchors if a[0] < i), key=lambda a: a[0])
            j1, p1 = min((a for a in anchors if a[0] > i), key=lambda a: a[0])
            given = p0 + (p1 - p0) * (i - j0) / (j1 - j0)  # noqa: PLW2901  # filling the gap
        filled.append(max(given, filled[-1] + MIN_GAP) if filled else given)
    span = filled[-1] or 1.0
    return [p / span for p in filled]


def pipeline_track(trace: dict[str, Any], timeline: dict[str, dt.datetime | None]) -> Scene:
    """The run's steps as stations spaced by when each wrote its record, with quota waits
    as hatched gaps and AI calls as markers."""
    run = trace["run"]
    start: dt.datetime | None = run.get("started_at")
    calls = trace.get("llm_calls", [])
    times: dict[str, float | None] = dict.fromkeys(k for k, _ in STATIONS)
    if start is not None:
        times["load_case"] = 0.0
        for key in ("extract", "run_checks", "persist_findings"):
            times[key] = _seconds(timeline.get(key), start)
        times.update(_ends(calls, start))
        if run.get("status") == "succeeded":
            times["await_decision"] = _seconds(run.get("finished_at"), start)
    last_timed = max((i for i, (k, _) in enumerate(STATIONS) if times[k] is not None), default=-1)
    total = max((t for t in times.values() if t is not None), default=0.0)

    positions = _spread([None if t is None or not total else t / total for t in times.values()])
    knots = [(t, positions[i]) for i, t in enumerate(times.values()) if t is not None]

    stations = [
        {
            "key": key,
            "label": label,
            "at": None if (at := times[key]) is None else round(at, 2),
            "done": i <= last_timed,
            "current": False,
            "p": round(positions[i], 4),
            "target": f"trace-step-{key}",
        }
        for i, (key, label) in enumerate(STATIONS)
    ]
    waits, markers = [], []
    if start is not None:
        for c in calls:
            end = _seconds(c.get("created_at"), start)
            if end is None:
                continue
            began = max(0.0, end - (c.get("latency_ms") or 0) / 1000)
            markers.append(
                {"p": round(_scale(knots, began), 4), "label": f"AI call: {c['purpose']}"}
            )
            wait = (c.get("quota_wait_ms") or 0) / 1000
            if wait > 0:
                waits.append(
                    {
                        "from": round(_scale(knots, max(0.0, began - wait)), 4),
                        "to": round(_scale(knots, began), 4),
                        "seconds": round(wait, 1),
                    }
                )
    timed = [s for s in stations if s["at"] is not None]
    label = (
        f"Pipeline track: {len(stations)} steps over {round(total, 1)} s"
        + (f", {sum(w['seconds'] for w in waits):.1f} s waiting for AI quota" if waits else "")
        + f", {_plural(len(markers), 'AI call')}. "
        + "; ".join(f"{s['label']} at {s['at']} s" for s in timed)
        + "."
    )
    payload = {"live": False, "stations": stations, "waits": waits, "markers": markers}
    return Scene("pipeline", "pipeline", label, payload, _pipeline_svg(payload))


def pipeline_live(progress: dict[str, Any]) -> Scene:
    """The investigation as it runs: steps evenly spaced, the current one lit."""
    steps = progress["steps"]
    count = max(len(steps) - 1, 1)
    stations = [
        {
            "key": s["key"],
            "label": s["label"],
            "at": None,
            "done": s["done"],
            "current": s["current"],
            "p": round(i / count, 4),
            "target": None,
        }
        for i, s in enumerate(steps)
    ]
    current = next((s for s in stations if s["current"]), None)
    waiting = progress.get("quota_resumes_at") is not None
    waits = (
        [{"from": current["p"], "to": min(1.0, current["p"] + 0.5 / count), "seconds": None}]
        if waiting and current
        else []
    )
    label = (
        f"Investigation in progress: {sum(s['done'] for s in stations)} of {len(stations)} steps"
        f" done{', now ' + current['label'].lower() if current else ''}"
        + (", waiting for AI quota" if waiting else "")
        + "."
    )
    payload = {"live": True, "stations": stations, "waits": waits, "markers": []}
    return Scene("pipeline", "pipeline-live", label, payload, _pipeline_svg(payload))


def _pipeline_svg(payload: dict[str, Any]) -> dict[str, Any]:
    x0, x1 = 48.0, 592.0  # room for the end labels

    def x(p: float) -> float:
        return round(x0 + (x1 - x0) * p, 1)

    return {
        "w": 640,
        "h": 112,
        "x0": x0,
        "x1": x1,
        "stations": [
            {**s, "x": x(s["p"]), "above": i % 2 == 0} for i, s in enumerate(payload["stations"])
        ],
        "waits": [
            {**w, "x": x(w["from"]), "width": max(2.0, x(w["to"]) - x(w["from"]))}
            for w in payload["waits"]
        ],
        "markers": [{**m, "x": x(m["p"])} for m in payload["markers"]],
    }


# ---------------------------------------------------------------- 5: the audit chain


PER_ROW = 6


def audit_chain(audit: dict[str, Any]) -> Scene | None:
    """The case's events as linked blocks; verified links solid, a broken one in red."""
    events = audit["items"]
    if not events:
        return None
    v = audit.get("verification")
    broken = v.get("broken_seq") if v else None
    blocks = []
    for e in events:
        seq = e["seq"]
        if v is None:
            state = "unchecked"
        elif broken is not None and seq == broken:
            state = "broken"
        elif seq <= v["verified_through_seq"] and (broken is None or seq < broken):
            state = "verified"
        else:
            state = "unchecked"
        digest = (e.get("payload") or {}).get("evidence_digest")
        blocks.append(
            {
                "seq": seq,
                "event": str(e["event_type"]).replace(".", " ").replace("_", " "),
                "hash": e["hash"][:8],
                "seal": digest[:8] if e["event_type"] == "run.completed" and digest else None,
                "state": state,
                "target": f"audit-{seq}",
            }
        )
    counts = {s: sum(b["state"] == s for b in blocks) for s in ("verified", "unchecked", "broken")}
    label = f"Audit chain: {_plural(len(blocks), 'event')} in order. " + (
        "Not verified yet."
        if v is None
        else f"{counts['verified']} verified, {counts['unchecked']} unchecked"
        + (f", broken at event {broken}" if counts["broken"] else "")
        + "."
    )
    return Scene("audit_chain", "audit-chain", label, {"blocks": blocks}, _audit_svg(blocks))


def _audit_svg(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    width, height, gap, row_h = 88.0, 44.0, 16.0, 64.0
    placed = []
    for i, b in enumerate(blocks):
        row, col = divmod(i, PER_ROW)
        placed.append(
            {
                **b,
                "short": _short(b["event"], 14),
                "x": 8 + col * (width + gap),
                "y": 8 + row * row_h,
            }
        )
    links = []
    for a, b in itertools.pairwise(placed):
        same_row = a["y"] == b["y"]
        links.append(
            {
                "state": b["state"],
                "x1": a["x"] + width if same_row else a["x"] + width / 2,
                "y1": a["y"] + height / 2 if same_row else a["y"] + height,
                "x2": b["x"] if same_row else b["x"] + width / 2,
                "y2": b["y"] + height / 2 if same_row else b["y"],
            }
        )
    rows = math.ceil(len(blocks) / PER_ROW)
    return {
        "w": 640,
        "h": 8 + rows * row_h,
        "bw": width,
        "bh": height,
        "blocks": placed,
        "links": links,
    }


# ---------------------------------------------------------------- 6: the document fan


def document_fan(
    invoice: dict[str, Any],
    supporting_docs: list[dict[str, Any]],
    *,
    page: int,
    highlight: str | None,
    previews: bool,
) -> Scene:
    """The case's documents as fanned page stacks; the page in the viewer sits forward."""
    pages = invoice.get("page_count") or 1
    suffix = f"&highlight={highlight}" if highlight else ""
    documents = [
        {
            "id": "doc-invoice",
            "label": "Invoice",
            "pages": [
                {
                    "n": n,
                    "label": f"Invoice page {n}",
                    "href": f"?page={n}{suffix}#documents" if previews else None,
                    "target": None if previews else "doc-invoice",
                    "forward": previews and n == page,
                }
                for n in range(1, pages + 1)
            ],
        }
    ]
    for s in supporting_docs:
        label = SUPPORTING_KIND.get(s["kind"], s["kind"])
        documents.append(
            {
                "id": f"doc-{s['id']}",
                "label": label,
                # One sheet: supporting documents have no page viewer, only their download
                # link, which the scene focuses but never follows (downloads are audited).
                "pages": [
                    {
                        "n": None,
                        "label": f"{label}: {s['description']}",
                        "href": None,
                        "target": f"doc-{s['id']}",
                        "forward": False,
                    }
                ],
            }
        )
    label = (
        f"Document fan: {_plural(len(documents), 'document')}. "
        + "; ".join(
            f"{d['label']}"
            + (f", {_plural(len(d['pages']), 'page')}" if d["id"] == "doc-invoice" else "")
            for d in documents
        )
        + (f". Page {page} of the invoice is in the viewer." if previews else ".")
    )
    return Scene(
        "document_fan", "document-fan", label, {"documents": documents}, _fan_svg(documents)
    )


def _fan_svg(documents: list[dict[str, Any]]) -> dict[str, Any]:
    sheet_w, sheet_h, step, gap = 40.0, 52.0, 9.0, 36.0
    x, groups = 12.0, []
    for d in documents:
        shown = d["pages"][:8]
        sheets = [
            {**p, "x": round(x + i * step, 1), "y": 14.0 if p["forward"] else 22.0}
            for i, p in enumerate(shown)
        ]
        width = sheet_w + step * (len(shown) - 1)
        groups.append(
            {
                **d,
                "sheets": sheets,
                "more": len(d["pages"]) - len(shown),
                "lx": round(x, 1),
                "width": width,
            }
        )
        x += width + gap
    return {"w": max(640.0, round(x, 1)), "h": 104, "sw": sheet_w, "sh": sheet_h, "groups": groups}
