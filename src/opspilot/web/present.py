"""How values read on screen (`07-design.md` §5, §8, §13): words of the trade, ledger figures.

Pure display: nothing here decides anything. Money has two decimals, separators
and no space after `$`; every difference carries a sign; a missing value is a
grey dash and a real zero prints as zero. The evidence rows turn each TruthLens
check's recorded comparison into Field · Invoice · Compared · Difference ·
Tolerance, reading only what the check stored.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

DASH = "—"
MINUS = "−"  # a real minus sign, not a hyphen

STATUS: dict[str, tuple[str, str, str]] = {  # state → (tone, glyph, label)
    "new": ("neutral", "◌", "Queued"),
    "investigating": ("neutral", "◌", "Investigating"),
    "investigation_failed": ("negative", "×", "Investigation failed"),
    "needs_review": ("warning", "●", "Needs review"),
    "pending_approval": ("neutral", "◷", "Waiting for approver"),
    "held_for_approver": ("warning", "‖", "Held: no approver"),
    "approved": ("neutral", "→", "Executing"),
    "closed": ("neutral", "", "Closed"),
}
OUTCOME = {  # closed by an executed action, in the trade's words
    "approve_for_payment": "Paid",
    "place_on_hold": "On hold",
    "reject_invoice": "Rejected",
    "draft_vendor_query": "Query drafted",
}
ACTION = {
    "approve_for_payment": "Approve payment",
    "place_on_hold": "Hold",
    "draft_vendor_query": "Vendor query",
    "reject_invoice": "Reject invoice",
}
ACTION_SUBMIT = {
    "approve_for_payment": "Submit payment for approval",
    "place_on_hold": "Submit hold for approval",
    "draft_vendor_query": "Submit vendor query for approval",
    "reject_invoice": "Submit rejection for approval",
}
FINDING = {
    "PRICE_MISMATCH": "Price above PO",
    "QTY_OVER_RECEIVED": "Billed more than received",
    "ARITHMETIC_MISMATCH": "Arithmetic doesn't add up",
    "TAX_MISCALCULATED": "Tax miscalculated",
    "DUPLICATE_INVOICE": "Possible duplicate",
    "VENDOR_NOT_APPROVED": "Vendor not approved",
    "APPROVAL_LIMIT_EXCEEDED": "Over the PO's approval level",
    "MISSING_OR_UNKNOWN_PO": "PO missing or unknown",
    "BANK_DETAILS_CHANGED": "Bank details changed",
    "EXTRACTION_UNCERTAIN": "Reading uncertain",
    "FORENSICS_SIGNAL": "Tamper signal",
}
FINDING_SHORT = {
    "PRICE_MISMATCH": "Price",
    "QTY_OVER_RECEIVED": "Quantity",
    "ARITHMETIC_MISMATCH": "Arithmetic",
    "TAX_MISCALCULATED": "Tax",
    "DUPLICATE_INVOICE": "Duplicate",
    "VENDOR_NOT_APPROVED": "Vendor",
    "APPROVAL_LIMIT_EXCEEDED": "Approval level",
    "MISSING_OR_UNKNOWN_PO": "PO",
    "BANK_DETAILS_CHANGED": "Bank",
    "EXTRACTION_UNCERTAIN": "Inconclusive",
    "FORENSICS_SIGNAL": "Tamper signal",
}
CHECK = {
    "po_lookup": "PO",
    "price": "Price",
    "quantity": "Quantity",
    "arithmetic": "Arithmetic",
    "tax": "Tax",
    "duplicate": "Duplicate",
    "vendor_approved": "Vendor",
    "approval_level": "Approval level",
    "bank_details": "Bank",
}
SUPPORTING_KIND = {
    "credit_note": "Credit note",
    "po_amendment": "PO amendment",
    "goods_receipt": "Goods receipt",
    "vendor_confirmation": "Vendor confirmation",
    "vendor_approval": "Vendor approval",
    "po_approval": "PO approval",
    "bank_verification": "Bank verification letter",
    "invoice_copy": "Invoice copy",
    "ledger_invoice_reference": "Ledger invoice reference",
    "bank_verification_record": "Bank verification record",
}
RESOLUTION = {
    "price_agreed": "Price agreed (PO amendment)",
    "credit_issued": "Credit issued",
    "additional_receipt": "Additional receipt",
    "vendor_approved": "Vendor approved",
    "po_approval_obtained": "PO approval obtained",
    "not_duplicate": "Not a duplicate",
    "bank_verified": "Bank details verified",
}
STRUCTURED = {
    "amended_unit_price": "Amended unit price",
    "credit_amount": "Credit amount",
    "received_quantity": "Received quantity (cumulative)",
    "vendor_code": "Vendor code named in the approval",
    "approval_level": "Approval level obtained",
    "both_invoice_numbers": "Invoice numbers named in the confirmation",
    "distinguishing_fact": "What makes them different",
}
CHANNEL = {
    "callback_to_master_contact": "Call-back to the vendor-master contact",
    "signed_letter_to_master_address": "Signed letter to the vendor-master address",
    "vendor_portal_change_request": "Change request in the vendor portal",
}
DECISION = {
    "submit": "Submitted",
    "approve": "Approved",
    "decline": "Declined",
    "dismiss": "Dismissed",
    "escalate": "Escalated",
    "reopen": "Reopened",
}
BASIS = {  # how a duplicate was matched
    "exact_number": "Same invoice number",
    "near_total_date_lines": "Near total and date, same lines",
}
METHOD = {"sku": "SKU", "description": "Description", "llm": "Suggested", "unmatched": "Unmatched"}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# Errors a form post can come back with, in words (the code travels in `?error=`, never text).
ERROR_WORDS = {
    "stale_version": (
        "Someone changed this case while you had it open. It has been reloaded;"
        " check it and try again."
    ),
    "invalid_transition": "That isn't possible in the case's current state.",
    "forbidden": "Your role can't do that.",
    "csrf_failed": "The form expired. Try again.",
    "self_approval": "You can't approve a case you submitted or changed.",
    "over_limit": "The amount is above your approval limit.",
    "tier_not_eligible": "This case needs a controller.",
    "action_not_allowed": "That action isn't allowed for this case's findings.",
    "restricted_finding": "A duplicate or bank-change finding must be resolved first.",
    "unresolved_findings": "Every finding must be resolved first.",
    "inconclusive_checks": "An inconclusive check needs a recheck first.",
    "recheck_required": "Only a recheck can clear this finding.",
    "stale_acceptance": "A resolution changed after you opened this review. Review again.",
    "acceptance_mismatch": "Accept every current resolution to approve.",
    "validation_failed": "Some fields are missing or invalid.",
    "resolution_evidence_missing": "Required evidence is missing.",
    "resolution_type_not_allowed": "That resolution type doesn't apply to this finding.",
    "resolution_check_failed": "The values don't pass the check.",
    "verification_invalid": "The bank verification isn't valid.",
    "file_too_large": "The file is over the size limit.",
    "unsupported_media_type": "Only PDF, PNG and JPEG files are accepted.",
    "already_uploaded": "That file has already been uploaded.",
    "rate_limited": "Too many uploads. Wait a minute and try again.",
    "not_ready": "The demo data isn't loaded yet.",
    "case_exists": "This invoice already has an open case.",
    "unknown": "That couldn't be done.",
}


def decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None


def money(value: Any) -> str:
    number = decimal(value)
    if number is None:
        return DASH
    text = f"${abs(number):,.2f}"
    return f"{MINUS}{text}" if number < 0 else text


def signed_money(value: Any) -> str:
    number = decimal(value)
    if number is None:
        return DASH
    return ("+" if number > 0 else MINUS if number < 0 else "") + f"${abs(number):,.2f}"


def percent(value: Any, *, signed: bool = False) -> str:
    number = decimal(value)
    if number is None:
        return DASH
    sign = ("+" if number > 0 else MINUS if number < 0 else "") if signed else ""
    shown = abs(number) if signed else number
    return f"{sign}{shown:,.2f}%"


def quantity(value: Any) -> str:
    number = decimal(value)
    if number is None:
        return DASH
    text = f"{number:,.3f}".rstrip("0").rstrip(".")
    return text or "0"


def relative(when: dt.datetime | None, now: dt.datetime | None = None) -> str:
    """'12 min', '2 h 10 min', 'Yesterday', or the date."""
    if when is None:
        return DASH
    now = now or dt.datetime.now(dt.UTC)
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 60:  # noqa: PLR2004
        return "Just now"
    minutes = seconds // 60
    if minutes < 60:  # noqa: PLR2004
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 24:  # noqa: PLR2004
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    if (now.date() - when.date()).days == 1:
        return "Yesterday"
    return when.date().isoformat()


def ago(when: dt.datetime | None, now: dt.datetime | None = None) -> str:
    """'12 min ago', 'just now', 'yesterday', or 'on 2026-10-20'."""
    shown = relative(when, now)
    if shown in {DASH, "Just now"}:
        return shown.lower() if shown != DASH else DASH
    if shown == "Yesterday":
        return "yesterday"
    return f"on {shown}" if shown[:4].isdigit() else f"{shown} ago"


def until(when: dt.datetime | None, now: dt.datetime | None = None) -> str:
    """'3 h 12 min' until a time ahead."""
    if when is None:
        return DASH
    minutes = max(0, int((when - (now or dt.datetime.now(dt.UTC))).total_seconds()) // 60)
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if hours else f"{rest} min"


def iso(when: dt.datetime | None) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC") if when else DASH


def status_pill(status: str, outcome: str | None = None) -> dict[str, str]:
    tone, glyph, label = STATUS.get(status, ("neutral", "", status))
    if status == "closed":
        label = OUTCOME.get(outcome or "", "Dismissed")
    return {"tone": tone, "glyph": glyph, "label": label}


def finding_list(types: list[str], limit: int = 3) -> str:
    words = [FINDING_SHORT.get(t, t) for t in types]
    shown = " · ".join(words[:limit])
    return shown + (f" +{len(words) - limit}" if len(words) > limit else "") if words else DASH


# ---------------------------------------------------------------- evidence rows


@dataclass(frozen=True)
class Row:
    field: str
    invoice: str
    compared_label: str
    compared: str
    difference: str = DASH
    tolerance: str = DASH
    breaks: bool = False


def _line(subject: str) -> str:
    return f" (line {subject.split(':', 1)[1]})" if subject.startswith("line:") else ""


def evidence_rows(check: dict[str, Any]) -> list[Row]:  # noqa: PLR0911  # one branch per check
    """Field · Invoice · Compared · Difference · Tolerance for one check's recorded comparison."""
    c = check.get("compared") or {}
    t = check.get("tolerance") or {}
    failed = check.get("status") == "fail"
    code = check.get("check_code")
    line = _line(check.get("subject_key", ""))
    if code == "price":
        return [
            Row(
                f"Unit price{line}",
                money(c.get("invoice_unit_price")),
                "PO",
                money(c.get("po_unit_price")),
                percent(c.get("variance_pct"), signed=True),
                f"≤ {percent(t.get('max_overcharge_pct'))}",
                failed,
            )
        ]
    if code == "quantity":
        billed = decimal(c.get("cumulative_billed"))
        received = decimal(c.get("received"))
        gap = billed - received if billed is not None and received is not None else None
        return [
            Row(
                f"Billed so far{line}",
                quantity(c.get("cumulative_billed")),
                "Received",
                quantity(c.get("received")),
                (("+" if gap > 0 else MINUS if gap < 0 else "") + quantity(abs(gap)))
                if gap is not None
                else DASH,
                "No over-billing",
                failed,
            )
        ]
    if code == "arithmetic" and "stated_line_total" in c:
        return [
            Row(
                f"Line total{line}",
                money(c.get("stated_line_total")),
                "Recomputed",
                money(c.get("computed")),
                signed_money(c.get("difference")),
                f"± {money(t.get('abs'))}",
                failed,
            )
        ]
    if code == "arithmetic":
        return [
            Row(
                "Subtotal",
                money(c.get("stated_subtotal")),
                "Sum of lines",
                money(c.get("lines_sum")),
                signed_money(c.get("subtotal_gap")),
                f"± {money(t.get('abs'))}",
                failed and (decimal(c.get("subtotal_gap")) or 0) != 0,
            ),
            Row(
                "Total",
                money(c.get("stated_total")),
                "Subtotal + tax",
                DASH,
                signed_money(c.get("total_gap")),
                f"± {money(t.get('abs'))}",
                failed and (decimal(c.get("total_gap")) or 0) != 0,
            ),
        ]
    if code == "tax":
        return [
            Row(
                "Tax",
                money(c.get("stated_tax")),
                f"Recomputed at {percent((decimal(c.get('rate')) or 0) * 100)}",
                money(c.get("computed_tax")),
                signed_money(c.get("difference")),
                f"± {money(t.get('abs'))}",
                failed,
            )
        ]
    if code == "duplicate":
        rows = [
            Row(
                "Invoice number",
                str(c.get("invoice_number") or DASH),
                "Ledger",
                str(m.get("ledger_invoice") or DASH),
                f"{BASIS.get(m.get('basis', ''), 'similar')}, dated {m.get('invoice_date', DASH)}",
                f"within {t.get('window_days', DASH)} days",
                failed,
            )
            for m in c.get("matches", [])
        ]
        return rows or [
            Row("Invoice number", str(c.get("invoice_number") or DASH), "Ledger", "No match")
        ]
    if code == "vendor_approved":
        return [
            Row(
                "Vendor",
                str(c.get("vendor_code") or DASH),
                "Vendor master",
                "Approved" if c.get("approved") else "Not approved",
                breaks=failed,
            )
        ]
    if code == "approval_level":
        return [
            Row(
                "Amount to approve",
                money(c.get("approval_amount")),
                f"PO level {c.get('po_approval_level', DASH)} limit",
                money(c.get("level_limit")) if c.get("level_limit") else "No limit",
                tolerance="Controller approved" if c.get("controller_approved") else DASH,
                breaks=failed,
            )
        ]
    if code == "bank_details":
        return [
            Row(
                "Bank account",
                str(c.get("invoice_bank_fp") or DASH),
                "Vendor master",
                str(c.get("vendor_master_bank_fp") or DASH),
                "Changed" if failed else "Same",
                breaks=failed,
            )
        ]
    if code == "po_lookup":
        return [
            Row(
                "PO number",
                str(c.get("invoice_po_number") or DASH),
                "ERP",
                str(c.get("result") or DASH).replace("_", " "),
                breaks=failed,
            )
        ]
    return [
        Row(str(k).replace("_", " ").capitalize(), str(v), DASH, DASH)
        for k, v in c.items()
        if not isinstance(v, dict | list)
    ]


def by_severity(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unresolved first, then high, medium, low; forensics signals last."""
    return sorted(
        findings,
        key=lambda f: (
            f["exception_type"] == "FORENSICS_SIGNAL",
            bool(f.get("resolved")),
            SEVERITY_ORDER.get(str(f.get("severity")), 9),
        ),
    )


def filters() -> dict[str, Any]:
    return {
        "money": money,
        "signed_money": signed_money,
        "percent": percent,
        "quantity": quantity,
        "relative": relative,
        "until": until,
        "ago": ago,
        "iso": iso,
        "finding_list": finding_list,
    }


def globals_() -> dict[str, Any]:
    return {
        "STATUS": STATUS,
        "ACTION": ACTION,
        "ACTION_SUBMIT": ACTION_SUBMIT,
        "FINDING": FINDING,
        "FINDING_SHORT": FINDING_SHORT,
        "CHECK": CHECK,
        "SUPPORTING_KIND": SUPPORTING_KIND,
        "RESOLUTION": RESOLUTION,
        "STRUCTURED": STRUCTURED,
        "CHANNEL": CHANNEL,
        "DECISION": DECISION,
        "METHOD": METHOD,
        "status_pill": status_pill,
        "ERROR_WORDS": ERROR_WORDS,
        "evidence_rows": evidence_rows,
        "by_severity": by_severity,
    }
