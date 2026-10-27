"""Render the procurement policy document from the configuration.

Every number in the document comes from `config.toml`, so the text people and
the agent read can never drift from what the checks enforce. Headings carry
stable section IDs ("3.1", "A.V-0017") that retrieval (P7) and citations use.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from opspilot.domain.enums import ActionType, FindingCode, ResolutionType
from opspilot.domain.money import Money, format_money
from opspilot.policy.loader import LoadedPolicy
from opspilot.policy.schema import AgentAllowlist, PolicyConfig, ResolutionRule

ACTION_WORDS: dict[ActionType, str] = {
    ActionType.APPROVE_FOR_PAYMENT: "approve for payment",
    ActionType.PLACE_ON_HOLD: "place on hold",
    ActionType.REJECT_INVOICE: "reject the invoice",
    ActionType.DRAFT_VENDOR_QUERY: "draft a vendor query",
}
_ACTION_ORDER = list(ActionType)

FINDING_WORDS: dict[FindingCode, str] = {
    FindingCode.MISSING_OR_UNKNOWN_PO: "Missing or unknown purchase order",
    FindingCode.PRICE_MISMATCH: "Unit price above the purchase order",
    FindingCode.QTY_OVER_RECEIVED: "Quantity billed above quantity received",
    FindingCode.ARITHMETIC_MISMATCH: "Invoice arithmetic does not add up",
    FindingCode.TAX_MISCALCULATED: "Tax calculated incorrectly",
    FindingCode.DUPLICATE_INVOICE: "Duplicate invoice",
    FindingCode.VENDOR_NOT_APPROVED: "Vendor not on the approved list",
    FindingCode.APPROVAL_LIMIT_EXCEEDED: "Amount above the purchase order's approval level",
    FindingCode.BANK_DETAILS_CHANGED: "Bank details differ from the vendor master",
    FindingCode.EXTRACTION_UNCERTAIN: "A check could not be completed (uncertain reading)",
    FindingCode.FORENSICS_SIGNAL: "Image tampering signal (experimental)",
}

EVIDENCE_WORDS: dict[str, str] = {
    "credit_note": "a credit note",
    "po_amendment": "an approved purchase order amendment",
    "goods_receipt": "a goods receipt",
    "vendor_confirmation": "a vendor confirmation naming both invoice numbers",
    "vendor_approval": "a procurement approval for the vendor",
    "po_approval": "a purchase order approval at the required level",
    "bank_verification": "the verification evidence document",
    "invoice_copy": "a replacement copy of the invoice",
    "bank_verification_record": "a bank verification record",
    "ledger_invoice_reference": "a reference to the matching previously processed invoice",
}

STRUCTURED_WORDS: dict[str, str] = {
    "amended_unit_price": "the amended unit price",
    "credit_amount": "the credit amount",
    "received_quantity": "the received quantity",
    "vendor_code": "the vendor code",
    "approval_level": "the approval level",
    "both_invoice_numbers": "both invoice numbers",
    "distinguishing_fact": "what distinguishes the two invoices",
}

RESOLUTION_WORDS: dict[ResolutionType, str] = {
    ResolutionType.PRICE_AGREED: "Price agreed",
    ResolutionType.CREDIT_ISSUED: "Credit issued",
    ResolutionType.ADDITIONAL_RECEIPT: "Additional goods received",
    ResolutionType.VENDOR_APPROVED: "Vendor approved",
    ResolutionType.PO_APPROVAL_OBTAINED: "Purchase order approval obtained",
    ResolutionType.NOT_DUPLICATE: "Not a duplicate",
    ResolutionType.BANK_VERIFIED: "Bank details verified",
}


@dataclass(frozen=True)
class VendorEntry:
    code: str
    name: str
    approved: bool


@dataclass(frozen=True)
class PolicyDocument:
    version: str
    config_sha256: str
    text: str

    @property
    def document_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _money(value: object) -> str:
    return format_money(Money.of(str(value)))


def _actions(actions: Iterable[ActionType]) -> str:
    return ", ".join(ACTION_WORDS[a] for a in sorted(actions, key=_ACTION_ORDER.index))


def _allowlist_rows(lists: AgentAllowlist) -> list[str]:
    rows = [
        f"| No findings, every check conclusive | {_actions(lists.clean)} |",
        f"| A check could not be completed | {_actions(lists.inconclusive)} |",
        f"| Duplicate invoice or changed bank details | {_actions(lists.restricted)} |",
        f"| Any other exception | {_actions(lists.other)} |",
    ]
    all_resolved = getattr(lists, "all_resolved", None)
    if all_resolved is not None:
        rows.append(f"| Every finding resolved with evidence | {_actions(all_resolved)} |")
    return rows


def _rule_line(rule: ResolutionRule) -> str:
    evidence = " and ".join(EVIDENCE_WORDS[e] for e in rule.evidence)
    details = " and ".join(STRUCTURED_WORDS[s] for s in rule.structured)
    suffix = f"; the analyst also records {details}" if details else ""
    return (
        f"- **{FINDING_WORDS[rule.finding]}**: {RESOLUTION_WORDS[rule.type]}. "
        f"Requires {evidence}{suffix}."
    )


def _sections(config: PolicyConfig, vendors: Sequence[VendorEntry]) -> list[str]:
    t = config.tolerances
    levels = config.approval_levels
    lines = [
        "## 1. Scope",
        "",
        "This policy governs how supplier invoices are checked against purchase orders, goods "
        "receipts and the vendor master before payment, and which actions may follow. No action "
        "on an invoice is taken without the approval of an authorised approver.",
        "",
        "## 2. Currency and rounding",
        "",
        "### 2.1 Currency",
        "",
        f"All amounts are in {config.currency}. Invoices in other currencies are out of scope.",
        "",
        "### 2.2 Rounding and boundaries",
        "",
        "Computed values are rounded half-up to two decimal places before comparison. "
        "Tolerances are inclusive on the accepted side: a value exactly at a tolerance passes, "
        "and anything beyond it is an exception. Price variance is (invoice unit price − PO unit "
        "price) ÷ PO unit price × 100, rounded to two decimal places.",
        "",
        "## 3. Three-way match tolerances",
        "",
        "### 3.1 Unit price against the purchase order",
        "",
        "An invoice unit price may exceed the purchase order unit price by at most "
        f"{t.unit_price_overcharge_pct}%. A unit price more than "
        f"{t.unit_price_undercharge_info_pct}% below the purchase order is recorded for "
        "information and is not an exception.",
        "",
        "### 3.2 Quantity against goods received",
        "",
        "The cumulative quantity billed for a purchase order line, including previously "
        "processed invoices, may not exceed the quantity received. There is no tolerance.",
        "",
        "### 3.3 Invoice arithmetic",
        "",
        "Each line total must equal quantity × unit price within "
        f"±{_money(t.line_arithmetic_abs)}. "
        f"The invoice total must equal the sum of its lines plus tax within "
        f"±{_money(t.invoice_total_abs)}.",
        "",
        "### 3.4 Tax",
        "",
        f"Stated tax must equal the tax computed at the vendor's registered rate within "
        f"±{_money(t.tax_abs)}.",
        "",
        "## 4. Duplicate invoices",
        "",
        "### 4.1 Exact duplicates",
        "",
        "An invoice is an exact duplicate when a previously processed invoice has the same "
        "vendor and the same invoice number after normalisation.",
        "",
        "### 4.2 Near duplicates",
        "",
        f"An invoice is a near duplicate when a previously processed invoice from the same vendor "
        f"has a total within ±{t.near_duplicate_total_pct}%, the same set of lines, and an invoice "
        f"date within {t.near_duplicate_window_days} days.",
        "",
        "## 5. Purchase order approval levels",
        "",
        f"- Level 1: up to {_money(levels.level_1_max)}.",
        f"- Level 2: up to {_money(levels.level_2_max)}.",
        f"- Level 3: above {_money(levels.level_2_max)}"
        + (
            ", and requires a recorded controller approval on the purchase order."
            if levels.level_3_requires_controller_approval
            else "."
        ),
        "",
        "An invoice whose total exceeds its purchase order's approval level without the "
        "required approval is an exception.",
        "",
        "## 6. Approver authority and escalation",
        "",
        f"Standard approvers may approve actions on invoices up to "
        f"{_money(config.approver_limits.standard)}. Controllers have no limit. The amount used "
        "is the larger of the invoice's stated total and the total recomputed from its lines and "
        "tax. A case above every available standard approver's limit is escalated to a controller; "
        "if no controller is available, it is held until one is. Every escalation is recorded. "
        "Nobody may approve a case they submitted or a proposal they changed.",
        "",
        "## 7. Changes to vendor bank details",
        "",
        "Bank details on an invoice that differ from the vendor master are treated as a possible "
        "fraud attempt. They may be accepted only after verification through one of these "
        "channels, using contact details taken from the vendor master and never from the invoice: "
        + ", ".join(c.value.replace("_", " ") for c in config.bank_verification.channels)
        + ".",
        "",
        "## 8. Permitted actions",
        "",
        "### 8.1 Actions the investigation agent may propose",
        "",
        "| Findings | Permitted proposals |",
        "|----------|---------------------|",
        *_allowlist_rows(config.allowlists.agent),
        "",
        "### 8.2 Actions an analyst may propose",
        "",
        "| Findings | Permitted proposals |",
        "|----------|---------------------|",
        *_allowlist_rows(config.allowlists.analyst),
        "",
        "Where several findings apply, only actions permitted for all of them may be proposed. "
        "Every proposal requires an authorised approver's approval before it takes effect.",
        "",
        "## 9. Resolving findings",
        "",
        "A finding is resolved only by a recorded resolution that cites the evidence below. A "
        "written explanation is always required and is never sufficient on its own.",
        "",
        *(_rule_line(r) for r in config.resolutions.rules),
        "",
        "The following can only be cleared by a successful recheck, never by a recorded "
        "resolution: "
        + "; ".join(FINDING_WORDS[c] for c in sorted(config.resolutions.recheck_only))
        + ".",
        "",
        "## 10. Approved vendors",
        "",
        "Invoices may be approved for payment only when the vendor is on the approved vendor "
        "list held in the vendor master. An invoice from a vendor that is not on the list is an "
        "exception and is resolved only with a procurement approval naming the vendor (section 9). "
        "Appendix A lists the approved vendors for this edition where a list is published.",
        "",
        "## Appendix A. Approved vendors",
        "",
    ]
    approved = [v for v in vendors if v.approved]
    if not approved:
        lines.append("No vendors are listed for this edition.")
    for vendor in sorted(approved, key=lambda v: v.code):
        lines += [
            f"### A.{vendor.code} {vendor.name}",
            "",
            f"{vendor.name} ({vendor.code}) is an approved vendor.",
            "",
        ]
    return lines


def render_policy(policy: LoadedPolicy, vendors: Sequence[VendorEntry] = ()) -> PolicyDocument:
    header = [
        f"# Procurement policy, version {policy.version}",
        "",
        f"Generated from configuration {policy.config_sha256[:12]}. Do not edit by hand.",
        "",
    ]
    text = "\n".join(header + _sections(policy.config, vendors)).rstrip() + "\n"
    return PolicyDocument(version=policy.version, config_sha256=policy.config_sha256, text=text)
