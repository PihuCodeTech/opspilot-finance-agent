import pytest

from opspilot.domain.bank import fingerprint
from opspilot.extraction.bank_tokens import (
    PossibleAccountLeakError,
    Tokeniser,
    find_account_numbers,
)

KEY = "test-key-for-tokens-000000000000000000000"


@pytest.mark.parametrize(
    "line",
    [
        "Bank account: 6348 1910 2752",
        "Pay to account: 9103 0759 7676",
        "Remit to 1514 2250 7358",
        "Account for payment 1234-5678-9012",
        "Settlement account\n1514 2250 7358",
        "Bank details: 021000021 004512398",
        "IBAN: GB82 WEST 1234 5698 7654 32",
        "acct 123456789012345 on file",
    ],
)
def test_account_numbers_are_found(line: str) -> None:
    tok = Tokeniser(KEY)
    out = tok.text(line)
    assert "BANK_ACCT[fp:" in out
    assert tok.tokens()


@pytest.mark.parametrize(
    "line",
    [
        "Invoice No. INV-2026-06938",
        "PO number PO-26-020003",
        "Reference 2026/30915",
        "Invoice date 2026-09-16",
        "Total due $2,641.08",
        "Subtotal 31890.92",
        "Pallet truck 6 5,315.1540 31,890.92",
        "Phone +1 555 0100",
        "Hex bolt M8 x 40mm, qty 2000",
        "Bill number A-48213",
    ],
)
def test_other_numbers_are_left_alone(line: str) -> None:
    assert find_account_numbers(line) == []
    assert Tokeniser(KEY).text(line) == line


def test_token_matches_the_fingerprint_used_everywhere() -> None:
    tok = Tokeniser(KEY)
    out = tok.text("Bank account: 6348 1910 2752")
    expected = fingerprint("634819102752", KEY)
    assert out == f"Bank account: {expected.token()}"
    assert tok.tokens() == [
        {"token": expected.token(), "bank_fp": expected.bank_fp, "last4": "2752"}
    ]


def test_nested_structures_are_tokenised() -> None:
    tok = Tokeniser(KEY)
    doc = {"texts": [{"text": "Bank account: 6348 1910 2752", "orig": "6348 1910 2752"}], "n": 3}
    out = tok.structure(doc)
    assert "6348" not in str(out)
    assert out["n"] == 3


@pytest.mark.parametrize(
    "line",
    [
        "weird 1234 5678-9012-3456 wrap",  # mixed separators
        "ref ABC1234567890123XYZ",  # digits glued into a word: could hide an account number
        "file e02aa679ad24ef54de0a89a2795038165fb4d.pdf",  # never tokenised, never stored
    ],
)
def test_a_surviving_digit_run_fails_closed(line: str) -> None:
    assert find_account_numbers(line) == [] or "-" in line
    with pytest.raises(PossibleAccountLeakError):
        Tokeniser(KEY).text(line)


def test_digit_runs_inside_our_own_tokens_are_not_leaks() -> None:
    """Regression: a fingerprint like 5d5951228773 holds ten digits in a row."""
    key = "key-000063-padding-000000000000000000"
    expected = fingerprint("6348 1910 2752", key)
    assert expected.bank_fp == "5d5951228773"
    tokenised = Tokeniser(key).text("Bank account: 6348 1910 2752")
    assert tokenised == f"Bank account: {expected.token()}"
