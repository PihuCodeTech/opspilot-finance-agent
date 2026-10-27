from pathlib import Path

import pytest

from opspilot.domain.bank import fingerprint, normalise_account
from opspilot.evaluation.generator.build import SplitSpec, generate
from opspilot.evaluation.generator.bundle import (
    BundleError,
    read_bundle,
    verify_bundle,
    world_document,
    write_bundle,
)
from opspilot.policy.loader import load_policy


def fake_pdf(html: str) -> bytes:
    """Stands in for WeasyPrint where Pango isn't installed; content-addressed like a real PDF."""
    return b"%PDF-1.7 test\n" + html.encode()


def test_fingerprint_is_keyed_short_and_separator_blind() -> None:
    a = fingerprint("6348 1910 2752", "key-one")
    assert a == fingerprint("6348-1910-2752", "key-one")
    assert len(a.bank_fp) == 12 and a.last4 == "2752"
    assert a.bank_fp != fingerprint("6348 1910 2752", "key-two").bank_fp
    assert a.token() == f"BANK_ACCT[fp:{a.bank_fp}]"
    assert a.masked() == "••••2752"
    with pytest.raises(ValueError):
        normalise_account("12-3")


def test_bundle_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    world = generate(SplitSpec("demo", 4004, 12, 0.5), load_policy().config)
    bundle = write_bundle(world, tmp_path, renderer=fake_pdf)
    assert verify_bundle(bundle) == []
    assert world_document(read_bundle(bundle)) == world_document(world)

    with pytest.raises(BundleError):
        write_bundle(world, tmp_path, renderer=fake_pdf)  # never overwrite

    (bundle / "labels.json").write_text("{}", encoding="utf-8")
    (bundle / "documents" / "stray.pdf").write_bytes(b"x")
    problems = verify_bundle(bundle)
    assert "changed: labels.json" in problems
    assert "unexpected: documents/stray.pdf" in problems
    with pytest.raises(BundleError):
        read_bundle(bundle)
