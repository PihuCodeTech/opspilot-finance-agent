"""The TruthLens checks (`04-prd.md` §3, definitions in `docs/datasets.md`).

Each check is a pure function of what was read (`InvoiceFacts`), the system
of record (`ErpContext`) and the policy. It returns pass, fail or
inconclusive with the compared values, the tolerance applied and evidence
references. A check whose input was read with low confidence is
`inconclusive`: it never passes and never raises an exception.

When the invoice has no valid PO, the PO-dependent checks (price, quantity,
approval level) have nothing to compare against and return no result at all.
"""

from collections import Counter
from decimal import Decimal

from opspilot.domain.enums import CheckCode, CheckStatus
from opspilot.domain.money import percent_change, round_money
from opspilot.policy.schema import PolicyConfig
from opspilot.truthlens.amounts import approval_amount, recomputed_total
from opspilot.truthlens.types import CheckOutcome, ErpContext, InvoiceFacts, InvoiceLineFacts

C, S = CheckCode, CheckStatus
HEADER = "header"


def _s(value: Decimal) -> str:
    return format(value, "f")


def _ref(entity: str, field: str, value: object, entity_id: object = None) -> dict[str, object]:
    return {
        "entity": entity,
        "id": str(entity_id) if entity_id is not None else None,
        "field": field,
        "value": value if isinstance(value, str | int | None) else str(value),
    }


def _uncertain(facts: InvoiceFacts, fields: list[str], policy: PolicyConfig) -> list[str]:
    floor = policy.extraction.min_field_confidence
    return [name for name in fields if facts.confidence_of(name) < floor]


def _inconclusive(code: CheckCode, subject: str, fields: list[str]) -> CheckOutcome:
    return CheckOutcome(
        code,
        subject,
        S.INCONCLUSIVE,
        {"uncertain_fields": fields},
        None,
        (),
        inconclusive_reason="low-confidence reading: " + ", ".join(fields),
    )


def _line_fields(line: InvoiceLineFacts, *names: str) -> list[str]:
    return [f"lines[{line.index}].{name}" for name in names]


def po_is_valid(facts: InvoiceFacts, ctx: ErpContext) -> bool:
    return (
        ctx.po is not None and ctx.po.vendor_code == ctx.vendor.code and facts.po_number is not None
    )


# ---------------------------------------------------------------- checks


def check_po_lookup(facts: InvoiceFacts, ctx: ErpContext, policy: PolicyConfig) -> CheckOutcome:
    if uncertain := _uncertain(facts, ["po_number"], policy):
        return _inconclusive(C.PO_LOOKUP, HEADER, uncertain)
    found_vendor = ctx.po.vendor_code if ctx.po is not None else None
    if facts.po_number is None:
        reason = "absent"
    elif ctx.po is None:
        reason = "unknown"
    elif found_vendor != ctx.vendor.code:
        reason = "other_vendor"
    else:
        reason = "ok"
    return CheckOutcome(
        C.PO_LOOKUP,
        HEADER,
        S.PASS if reason == "ok" else S.FAIL,
        {
            "invoice_po_number": facts.po_number,
            "result": reason,
            "po_vendor": found_vendor,
            "invoice_vendor": ctx.vendor.code,
        },
        None,
        (
            _ref("invoice", "po_number", facts.po_number),
            *((_ref("purchase_order", "vendor", found_vendor, ctx.po.id),) if ctx.po else ()),
        ),
    )


def check_prices(facts: InvoiceFacts, ctx: ErpContext, policy: PolicyConfig) -> list[CheckOutcome]:
    if not po_is_valid(facts, ctx) or ctx.po is None:
        return []
    limit = policy.tolerances.unit_price_overcharge_pct
    info = policy.tolerances.unit_price_undercharge_info_pct
    out = []
    for line in facts.lines:
        line_no = ctx.line_mapping.get(line.index)
        po_line = ctx.po.line(line_no) if line_no is not None else None
        if po_line is None:
            continue  # unmatched lines are surfaced by matching, not priced
        subject = f"line:{line.index}"
        if uncertain := _uncertain(facts, _line_fields(line, "unit_price"), policy):
            out.append(_inconclusive(C.PRICE, subject, uncertain))
            continue
        variance = percent_change(line.unit_price, po_line.unit_price)
        out.append(
            CheckOutcome(
                C.PRICE,
                subject,
                S.FAIL if variance > limit else S.PASS,
                {
                    "invoice_unit_price": _s(line.unit_price),
                    "po_unit_price": _s(po_line.unit_price),
                    "variance_pct": _s(variance),
                    "undercharge_info": variance < -info,
                },
                {"max_overcharge_pct": _s(limit)},
                (
                    _ref("invoice", f"lines[{line.index}].unit_price", line.unit_price),
                    _ref("po_line", "unit_price", po_line.unit_price, po_line.id),
                ),
            )
        )
    return out


def check_quantities(
    facts: InvoiceFacts, ctx: ErpContext, policy: PolicyConfig
) -> list[CheckOutcome]:
    if not po_is_valid(facts, ctx) or ctx.po is None:
        return []
    out = []
    for line in facts.lines:
        line_no = ctx.line_mapping.get(line.index)
        po_line = ctx.po.line(line_no) if line_no is not None else None
        if po_line is None:
            continue
        subject = f"line:{line.index}"
        if uncertain := _uncertain(facts, _line_fields(line, "quantity"), policy):
            out.append(_inconclusive(C.QUANTITY, subject, uncertain))
            continue
        received = ctx.received.get(po_line.line_no, Decimal(0))
        before = ctx.billed_before.get(po_line.line_no, Decimal(0))
        cumulative = before + line.quantity
        out.append(
            CheckOutcome(
                C.QUANTITY,
                subject,
                S.FAIL if cumulative > received else S.PASS,
                {
                    "invoice_quantity": _s(line.quantity),
                    "billed_before": _s(before),
                    "cumulative_billed": _s(cumulative),
                    "received": _s(received),
                },
                {"over_billing_allowed": "0"},
                (
                    _ref("invoice", f"lines[{line.index}].quantity", line.quantity),
                    _ref("po_line", "quantity_received", received, po_line.id),
                ),
            )
        )
    return out


def check_arithmetic(
    facts: InvoiceFacts, ctx: ErpContext, policy: PolicyConfig
) -> list[CheckOutcome]:
    t = policy.tolerances
    out = []
    for line in facts.lines:
        subject = f"line:{line.index}"
        if uncertain := _uncertain(
            facts, _line_fields(line, "quantity", "unit_price", "line_total"), policy
        ):
            out.append(_inconclusive(C.ARITHMETIC, subject, uncertain))
            continue
        computed = round_money(line.quantity * line.unit_price)
        difference = line.line_total - computed
        out.append(
            CheckOutcome(
                C.ARITHMETIC,
                subject,
                S.FAIL if abs(difference) > t.line_arithmetic_abs else S.PASS,
                {
                    "stated_line_total": _s(line.line_total),
                    "computed": _s(computed),
                    "difference": _s(difference),
                },
                {"abs": _s(t.line_arithmetic_abs)},
                (_ref("invoice", f"lines[{line.index}].line_total", line.line_total),),
            )
        )
    header_fields = ["subtotal", "tax", "total"] + [
        f"lines[{ln.index}].line_total" for ln in facts.lines
    ]
    if uncertain := _uncertain(facts, header_fields, policy):
        out.append(_inconclusive(C.ARITHMETIC, HEADER, uncertain))
        return out
    lines_sum = sum((ln.line_total for ln in facts.lines), Decimal(0))
    subtotal_gap = facts.subtotal - lines_sum
    total_gap = facts.total - (facts.subtotal + facts.tax)
    failed = abs(subtotal_gap) > t.invoice_total_abs or abs(total_gap) > t.invoice_total_abs
    out.append(
        CheckOutcome(
            C.ARITHMETIC,
            HEADER,
            S.FAIL if failed else S.PASS,
            {
                "lines_sum": _s(lines_sum),
                "stated_subtotal": _s(facts.subtotal),
                "subtotal_gap": _s(subtotal_gap),
                "stated_tax": _s(facts.tax),
                "stated_total": _s(facts.total),
                "total_gap": _s(total_gap),
            },
            {"abs": _s(t.invoice_total_abs)},
            (_ref("invoice", "subtotal", facts.subtotal), _ref("invoice", "total", facts.total)),
        )
    )
    return out


def check_tax(facts: InvoiceFacts, ctx: ErpContext, policy: PolicyConfig) -> CheckOutcome:
    if uncertain := _uncertain(facts, ["subtotal", "tax"], policy):
        return _inconclusive(C.TAX, HEADER, uncertain)
    computed = round_money(facts.subtotal * ctx.vendor.tax_rate)
    difference = facts.tax - computed
    return CheckOutcome(
        C.TAX,
        HEADER,
        S.FAIL if abs(difference) > policy.tolerances.tax_abs else S.PASS,
        {
            "stated_tax": _s(facts.tax),
            "taxable_base": _s(facts.subtotal),
            "rate": _s(ctx.vendor.tax_rate),
            "computed_tax": _s(computed),
            "difference": _s(difference),
        },
        {"abs": _s(policy.tolerances.tax_abs)},
        (
            _ref("invoice", "tax", facts.tax),
            _ref("vendor", "tax_rate", ctx.vendor.tax_rate, ctx.vendor.id),
        ),
    )


def normalise_invoice_number(number: str) -> str:
    return "".join(ch for ch in number.lower() if ch.isalnum())


def _line_set(lines: object) -> Counter[tuple[str, Decimal]]:
    return Counter((ln.sku or ln.description, ln.quantity) for ln in lines)  # type: ignore[attr-defined]


def check_duplicate(facts: InvoiceFacts, ctx: ErpContext, policy: PolicyConfig) -> CheckOutcome:
    fields = ["invoice_number", "invoice_date", "total"] + [
        f"lines[{ln.index}].quantity" for ln in facts.lines
    ]
    if uncertain := _uncertain(facts, fields, policy):
        return _inconclusive(C.DUPLICATE, HEADER, uncertain)
    t = policy.tolerances
    number = normalise_invoice_number(facts.number)
    matches = []
    for ledger in ctx.vendor_ledger:
        if normalise_invoice_number(ledger.number) == number:
            matches.append((ledger, "exact_number"))
        elif (
            ledger.total != 0
            and abs(percent_change(facts.total, ledger.total)) <= t.near_duplicate_total_pct
            and abs((facts.invoice_date - ledger.invoice_date).days) <= t.near_duplicate_window_days
            and _line_set(facts.lines) == _line_set(ledger.lines)
        ):
            matches.append((ledger, "near_total_date_lines"))
    return CheckOutcome(
        C.DUPLICATE,
        HEADER,
        S.FAIL if matches else S.PASS,
        {
            "invoice_number": facts.number,
            "normalised": number,
            "matches": [
                {
                    "ledger_invoice": ledger.number,
                    "basis": basis,
                    "total": _s(ledger.total),
                    "invoice_date": ledger.invoice_date.isoformat(),
                }
                for ledger, basis in matches
            ],
        },
        {
            "near_total_pct": _s(t.near_duplicate_total_pct),
            "window_days": t.near_duplicate_window_days,
        },
        tuple(
            _ref("ledger_invoice", "invoice_number", ledger.number, ledger.id)
            for ledger, _ in matches
        ),
    )


def check_vendor(facts: InvoiceFacts, ctx: ErpContext, policy: PolicyConfig) -> CheckOutcome:
    if uncertain := _uncertain(facts, ["vendor"], policy):
        return _inconclusive(C.VENDOR_APPROVED, HEADER, uncertain)
    return CheckOutcome(
        C.VENDOR_APPROVED,
        HEADER,
        S.PASS if ctx.vendor.approved else S.FAIL,
        {"vendor_code": ctx.vendor.code, "approved": ctx.vendor.approved},
        None,
        (_ref("vendor", "is_approved", str(ctx.vendor.approved).lower(), ctx.vendor.id),),
    )


def check_approval_level(
    facts: InvoiceFacts, ctx: ErpContext, policy: PolicyConfig
) -> list[CheckOutcome]:
    if not po_is_valid(facts, ctx) or ctx.po is None:
        return []
    fields = ["total"] + [
        f"lines[{ln.index}].{f}" for ln in facts.lines for f in ("quantity", "unit_price")
    ]
    if uncertain := _uncertain(facts, fields, policy):
        return [_inconclusive(C.APPROVAL_LEVEL, HEADER, uncertain)]
    levels = policy.approval_levels
    amount = approval_amount(facts, ctx.vendor.tax_rate)
    limit = {1: levels.level_1_max, 2: levels.level_2_max}.get(ctx.po.approval_level)
    over_level = limit is not None and amount > limit
    needs_controller = amount > levels.level_2_max and not ctx.po.controller_approved
    return [
        CheckOutcome(
            C.APPROVAL_LEVEL,
            HEADER,
            S.FAIL if over_level or needs_controller else S.PASS,
            {
                "approval_amount": _s(amount),
                "stated_total": _s(facts.total),
                "recomputed_total": _s(recomputed_total(facts, ctx.vendor.tax_rate)),
                "po_approval_level": ctx.po.approval_level,
                "level_limit": _s(limit) if limit else None,
                "controller_approved": ctx.po.controller_approved,
            },
            {"level_1_max": _s(levels.level_1_max), "level_2_max": _s(levels.level_2_max)},
            (_ref("purchase_order", "approval_level", ctx.po.approval_level, ctx.po.id),),
        )
    ]


def check_bank(facts: InvoiceFacts, ctx: ErpContext, policy: PolicyConfig) -> CheckOutcome:
    if facts.bank_fp is None:
        return CheckOutcome(
            C.BANK_DETAILS,
            HEADER,
            S.INCONCLUSIVE,
            {"invoice_bank_fp": None},
            None,
            (),
            inconclusive_reason="no bank account found on the invoice",
        )
    if uncertain := _uncertain(facts, ["bank_account"], policy):
        return _inconclusive(C.BANK_DETAILS, HEADER, uncertain)
    same = facts.bank_fp == ctx.vendor.current_bank_fp
    return CheckOutcome(
        C.BANK_DETAILS,
        HEADER,
        S.PASS if same else S.FAIL,
        {"invoice_bank_fp": facts.bank_fp, "vendor_master_bank_fp": ctx.vendor.current_bank_fp},
        None,
        (
            _ref("invoice", "bank_account", f"BANK_ACCT[fp:{facts.bank_fp}]"),
            _ref("vendor_bank_account", "bank_fp", ctx.vendor.current_bank_fp, ctx.vendor.id),
        ),
    )


def run_all(facts: InvoiceFacts, ctx: ErpContext, policy: PolicyConfig) -> list[CheckOutcome]:
    """Every check, in a fixed order. Pure: same inputs, same outputs."""
    return [
        check_po_lookup(facts, ctx, policy),
        *check_prices(facts, ctx, policy),
        *check_quantities(facts, ctx, policy),
        *check_arithmetic(facts, ctx, policy),
        check_tax(facts, ctx, policy),
        check_duplicate(facts, ctx, policy),
        check_vendor(facts, ctx, policy),
        *check_approval_level(facts, ctx, policy),
        check_bank(facts, ctx, policy),
    ]
