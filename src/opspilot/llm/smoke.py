"""One tiny live call to check the provider, quota gate and recording end to end.

Uses only synthetic text (no document, no bank data). Run once with your free
Gemini key; the response is saved as a test fixture so tests replay it.
"""

from pathlib import Path

from pydantic import BaseModel

from opspilot.config import LlmMode, Settings
from opspilot.llm.client import StructuredResult
from opspilot.llm.factory import build_client
from opspilot.llm.types import LlmRequest, Message

SMOKE_TEXT = (
    "Invoice INV-2026-00418 from Halden Industrial Supply. "
    "2 lines: Hex bolts M8x40 zinc, 1000 at 0.42; Flat washers M8 SS, 1000 at 0.09. "
    "Subtotal 510.00, tax 42.08, total due 552.08. Bank account: BANK_ACCT[fp:3f9a1c2b7d10]."
)


class SmokeAnswer(BaseModel):
    invoice_number: str
    line_count: int
    total: str


def smoke_request() -> LlmRequest:
    return LlmRequest(
        purpose="smoke",
        system="Extract the requested fields from the invoice text. Reply with JSON only.",
        messages=(Message("user", text=SMOKE_TEXT),),
        max_output_tokens=256,
    )


def run_smoke(settings: Settings, fixtures_dir: Path) -> tuple[StructuredResult[SmokeAnswer], int]:
    client = build_client(replace_mode(settings), fixtures_dir)
    result = client.generate_structured(smoke_request(), SmokeAnswer, max_retries=1)
    used = client.quota.used_today() if client.quota else 0
    return result, used


def replace_mode(settings: Settings) -> Settings:
    return settings.model_copy(update={"llm_mode": LlmMode.RECORD})
