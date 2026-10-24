"""Bank account fingerprints (`06-database.md` §6).

A full account number is never stored, logged or sent anywhere: it is reduced
at once to a short keyed fingerprint (`bank_fp`, the first 12 hex characters of
HMAC-SHA256) plus its last 4 digits. TruthLens and the baseline both compare
the same `bank_fp`, so neither has privileged bank data.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass

FINGERPRINT_LENGTH = 12
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]")
_MIN_LENGTH = 6


@dataclass(frozen=True)
class BankFingerprint:
    bank_fp: str
    last4: str

    def token(self) -> str:
        """The placeholder that replaces the number in any text a model or the baseline reads."""
        return f"BANK_ACCT[fp:{self.bank_fp}]"

    def masked(self) -> str:
        return f"••••{self.last4}"


def normalise_account(number: str) -> str:
    """Remove spaces, dashes and other separators; upper-case letters (IBAN-style)."""
    normalised = _NON_ALNUM.sub("", number).upper()
    if len(normalised) < _MIN_LENGTH:
        raise ValueError("too short to be an account number")
    return normalised


def fingerprint(number: str, key: str) -> BankFingerprint:
    normalised = normalise_account(number)
    digest = hmac.new(key.encode("utf-8"), normalised.encode("utf-8"), hashlib.sha256).hexdigest()
    return BankFingerprint(bank_fp=digest[:FINGERPRINT_LENGTH], last4=normalised[-4:])
