import pytest

from opspilot.config import Settings
from opspilot.db.roles import RoleProvisioningError, role_logins
from opspilot.policy.chunks import split_sections
from opspilot.policy.document import VendorEntry, render_policy
from opspilot.policy.loader import load_policy

ROLE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://opspilot_app:pa@db/x",
    "DATABASE_URL_BASELINE_RO": "postgresql+psycopg://opspilot_baseline_ro:pb@db/x",
    "DATABASE_URL_SCORER": "postgresql+psycopg://opspilot_scorer:ps@db/x",
    "DATABASE_URL_VERIFIER": "postgresql+psycopg://opspilot_verifier:pv@db/x",
}


def test_role_logins_read_from_each_url(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in ROLE_ENV.items():
        monkeypatch.setenv(key, value)
    logins = {login.role: login.password for login in role_logins(Settings())}
    assert logins == {
        "opspilot_app": "pa",
        "opspilot_baseline_ro": "pb",
        "opspilot_scorer": "ps",
        "opspilot_verifier": "pv",
    }


def test_role_logins_refuse_a_mismatched_user(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in ROLE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL_SCORER", "postgresql+psycopg://opspilot_owner:x@db/x")
    with pytest.raises(RoleProvisioningError, match="must connect as opspilot_scorer"):
        role_logins(Settings())


def test_role_logins_require_every_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", ROLE_ENV["DATABASE_URL"])
    with pytest.raises(RoleProvisioningError, match="DATABASE_URL_VERIFIER is not set"):
        role_logins(Settings())


def test_policy_document_splits_into_citable_sections() -> None:
    text = render_policy(load_policy(), [VendorEntry("V-0003", "Corvid Freight", True)]).text
    chunks = {c.section_id: c for c in split_sections(text)}
    assert {"1", "2.1", "3.1", "3.4", "4.2", "6", "8.1", "9", "A.V-0003"} <= set(chunks)
    assert "3" not in chunks  # a heading with no text of its own makes no chunk
    assert "2.00%" in chunks["3.1"].text
    assert chunks["A.V-0003"].title == "Corvid Freight"
