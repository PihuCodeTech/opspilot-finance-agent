from opspilot.domain.enums import ResolutionType
from opspilot.policy.document import (
    EVIDENCE_WORDS,
    RESOLUTION_WORDS,
    STRUCTURED_WORDS,
    VendorEntry,
    render_policy,
)
from opspilot.policy.loader import load_policy
from opspilot.policy.schema import EVIDENCE_ITEMS, STRUCTURED_FIELDS

POLICY = load_policy()


def test_document_states_the_configured_values() -> None:
    text = render_policy(POLICY).text
    for expected in [
        "at most 2.00%",
        "within ±$0.01",
        "within ±$0.05",
        "within ±0.50%",
        "within 30 days",
        "Level 1: up to $10,000.00",
        "Level 2: up to $50,000.00",
        "up to $10,000.00. Controllers have no limit",
        "contact details taken from the vendor master and never from the invoice",
        "never sufficient on its own",
    ]:
        assert expected in text, expected


def test_section_ids_for_retrieval_and_citation() -> None:
    text = render_policy(POLICY).text
    for heading in ["### 3.1 ", "### 3.4 ", "### 4.2 ", "## 6. ", "### 8.1 ", "### 8.2 ", "## 9. "]:
        assert heading in text, heading


def test_rendering_is_deterministic() -> None:
    assert render_policy(POLICY).document_sha256 == render_policy(POLICY).document_sha256


def test_appendix_lists_only_approved_vendors_in_code_order() -> None:
    vendors = [
        VendorEntry("V-0020", "Halden Industrial Supply", approved=True),
        VendorEntry("V-0003", "Corvid Freight", approved=True),
        VendorEntry("V-0011", "Unlisted Traders", approved=False),
    ]
    text = render_policy(POLICY, vendors).text
    assert text.index("### A.V-0003 Corvid Freight") < text.index("### A.V-0020 Halden")
    assert "Unlisted Traders" not in text


def test_every_configurable_term_has_words() -> None:
    assert set(EVIDENCE_WORDS) == set(EVIDENCE_ITEMS)
    assert set(STRUCTURED_WORDS) == set(STRUCTURED_FIELDS)
    assert set(RESOLUTION_WORDS) == set(ResolutionType)
