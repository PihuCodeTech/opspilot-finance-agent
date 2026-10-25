"""Docling parsing of real generated invoices (no database)."""

import json

import pytest
from tests.docling_support import FIXTURES, KEY, preprocessor, require_docling, truth

from opspilot.domain.bank import fingerprint
from opspilot.extraction.preprocess import Preprocessor, UnsupportedDocumentError

pytestmark = pytest.mark.docling


@pytest.mark.parametrize("name", ["demo-classic", "demo-ledger"])
def test_parse_keeps_the_facts_and_drops_the_bank_number(name: str) -> None:
    require_docling()
    facts = truth(name)
    parsed = preprocessor().parse(FIXTURES / f"{name}.pdf", "application/pdf")

    assert facts["number"] in parsed.text
    assert facts["po_number"] in parsed.text
    total = f"{float(facts['total']):,.2f}"
    assert total in parsed.text or facts["total"] in parsed.text
    assert "|" in parsed.text  # line items come through as a table

    fp = fingerprint(facts["bank_account"], KEY)
    digits = facts["bank_account"].replace(" ", "")
    everything = parsed.text + json.dumps(parsed.structure) + json.dumps(parsed.coordinates)
    assert fp.token() in parsed.text
    assert digits not in everything.replace(" ", "")
    assert parsed.bank_tokens == [{"token": fp.token(), "bank_fp": fp.bank_fp, "last4": fp.last4}]
    assert name not in json.dumps(parsed.structure)  # the stored file name never enters the parse

    assert parsed.coordinates["items"] and parsed.coordinates["table_cells"]
    first = parsed.coordinates["items"][0]
    assert first["page"] == 1 and len(first["bbox"]) == 4


def test_parsing_is_deterministic() -> None:
    require_docling()
    path = FIXTURES / "demo-classic.pdf"
    a = preprocessor().parse(path, "application/pdf")
    b = preprocessor().parse(path, "application/pdf")
    assert a.text == b.text and a.structure == b.structure


def test_images_wait_for_the_ocr_path() -> None:
    with pytest.raises(UnsupportedDocumentError):
        Preprocessor(KEY).parse(FIXTURES / "demo-classic.pdf", "image/png")
