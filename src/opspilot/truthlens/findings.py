"""From check outcomes to findings (`04-prd.md` §3).

A failed check becomes the exception it detects; an inconclusive check
becomes `EXTRACTION_UNCERTAIN` for the same subject. Findings come only from
here, never from the model.
"""

from dataclasses import dataclass

from opspilot.domain.enums import CheckCode, CheckStatus, FindingCode, Severity
from opspilot.truthlens.types import CheckOutcome

EXCEPTION_FOR: dict[CheckCode, FindingCode] = {
    CheckCode.PO_LOOKUP: FindingCode.MISSING_OR_UNKNOWN_PO,
    CheckCode.PRICE: FindingCode.PRICE_MISMATCH,
    CheckCode.QUANTITY: FindingCode.QTY_OVER_RECEIVED,
    CheckCode.ARITHMETIC: FindingCode.ARITHMETIC_MISMATCH,
    CheckCode.TAX: FindingCode.TAX_MISCALCULATED,
    CheckCode.DUPLICATE: FindingCode.DUPLICATE_INVOICE,
    CheckCode.VENDOR_APPROVED: FindingCode.VENDOR_NOT_APPROVED,
    CheckCode.APPROVAL_LEVEL: FindingCode.APPROVAL_LIMIT_EXCEEDED,
    CheckCode.BANK_DETAILS: FindingCode.BANK_DETAILS_CHANGED,
}

SEVERITY: dict[FindingCode, Severity] = {
    FindingCode.BANK_DETAILS_CHANGED: Severity.HIGH,
    FindingCode.DUPLICATE_INVOICE: Severity.HIGH,
    FindingCode.VENDOR_NOT_APPROVED: Severity.HIGH,
    FindingCode.APPROVAL_LIMIT_EXCEEDED: Severity.HIGH,
    FindingCode.MISSING_OR_UNKNOWN_PO: Severity.MEDIUM,
    FindingCode.PRICE_MISMATCH: Severity.MEDIUM,
    FindingCode.QTY_OVER_RECEIVED: Severity.MEDIUM,
    FindingCode.EXTRACTION_UNCERTAIN: Severity.MEDIUM,
    FindingCode.ARITHMETIC_MISMATCH: Severity.LOW,
    FindingCode.TAX_MISCALCULATED: Severity.LOW,
    FindingCode.FORENSICS_SIGNAL: Severity.LOW,
}


@dataclass(frozen=True)
class FindingDraft:
    code: FindingCode
    subject_key: str
    severity: Severity
    outcome_indexes: tuple[int, ...]  # positions in the outcome list this finding cites


def derive_findings(outcomes: list[CheckOutcome]) -> list[FindingDraft]:
    """One finding per (code, subject); each cites the check outcomes behind it."""
    grouped: dict[tuple[FindingCode, str], list[int]] = {}
    for index, outcome in enumerate(outcomes):
        if outcome.status is CheckStatus.FAIL:
            code = EXCEPTION_FOR[outcome.check_code]
        elif outcome.status is CheckStatus.INCONCLUSIVE:
            code = FindingCode.EXTRACTION_UNCERTAIN
        else:
            continue
        grouped.setdefault((code, outcome.subject_key), []).append(index)
    return [
        FindingDraft(code, subject, SEVERITY[code], tuple(indexes))
        for (code, subject), indexes in sorted(
            grouped.items(), key=lambda kv: (kv[0][0].value, kv[0][1])
        )
    ]
