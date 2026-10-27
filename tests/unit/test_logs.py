from typing import Any

from opspilot.logs import mask_digits, redact


def test_long_digit_runs_are_masked_to_last_four() -> None:
    assert mask_digits("acct 12345678901234 ok") == "acct ••••1234 ok"
    assert mask_digits("IBAN 1234 5678 9012 3456") == "IBAN ••••3456"


def test_short_numbers_are_kept() -> None:
    assert mask_digits("invoice total 1234.50 for 7 items") == "invoice total 1234.50 for 7 items"


def test_sensitive_keys_are_redacted_at_any_depth() -> None:
    event: dict[str, Any] = {
        "event": "x",
        "password": "hunter2",
        "nested": {"api_key": "abc", "note": "acct 99998888777766"},
    }
    out = redact(None, "info", event)
    assert out["password"] == "[redacted]"
    assert out["nested"]["api_key"] == "[redacted]"
    assert out["nested"]["note"] == "acct ••••7766"


def test_iso_dates_are_not_masked() -> None:
    assert mask_digits("due 2026-10-24, paid 2026-10-24T17:51:58Z") == (
        "due 2026-10-24, paid 2026-10-24T17:51:58Z"
    )


def test_uuids_are_not_masked() -> None:
    run_id = "d43d6cdd-1234-4567-876c-158c2d0cc816"
    assert mask_digits(f"run {run_id} failed") == f"run {run_id} failed"
    assert mask_digits(f"{run_id} acct 12345678901234") == f"{run_id} acct ••••1234"
