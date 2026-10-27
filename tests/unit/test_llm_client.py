"""The LLM client without a database or network."""

import datetime as dt
from pathlib import Path

import pytest
from pydantic import BaseModel
from tests.llm_fakes import FakeProvider, rate_limited, text

from opspilot.config import LlmMode
from opspilot.llm.client import LlmClient
from opspilot.llm.errors import (
    ProviderUnavailableError,
    ReplayMissError,
    SensitiveDataError,
    StructuredOutputError,
)
from opspilot.llm.quota import day_window, minute_window
from opspilot.llm.types import LlmRequest, Message


class Answer(BaseModel):
    total: str
    lines: int


class FreeGate:
    """Quota gate stand-in: always grants (the real gate is tested against Postgres)."""

    def __init__(self) -> None:
        self.taken = 0

    def acquire(self) -> int:
        self.taken += 1
        return 0


def request(body: str = "Total due $2,641.08, bank BANK_ACCT[fp:3f9a1c2b7d10]") -> LlmRequest:
    return LlmRequest("smoke", "You extract invoice fields.", (Message("user", text=body),))


def live(provider: FakeProvider, gate: FreeGate | None = None, **kw: object) -> LlmClient:
    return LlmClient(provider, LlmMode.RECORD, gate or FreeGate(), sleep=lambda _s: None, **kw)  # type: ignore[arg-type]


def test_record_then_replay_without_touching_the_provider(tmp_path: Path) -> None:
    recorder = live(FakeProvider([text("hello")]), fixtures_dir=tmp_path)
    first = recorder.generate(request())
    assert first.response.text == "hello" and not first.replayed
    assert (tmp_path / f"{first.request_sha256}.json").exists()

    never = FakeProvider([])  # replay must not call it
    replayer = LlmClient(never, LlmMode.REPLAY, None, fixtures_dir=tmp_path)
    again = replayer.generate(request())
    assert again.replayed and again.response.text == "hello" and never.seen == []


def test_replay_miss_is_loud(tmp_path: Path) -> None:
    client = LlmClient(FakeProvider([]), LlmMode.REPLAY, None, fixtures_dir=tmp_path)
    with pytest.raises(ReplayMissError):
        client.generate(request("something never recorded"))


def test_request_hash_depends_on_provider_and_content(tmp_path: Path) -> None:
    a = live(FakeProvider([]), fixtures_dir=tmp_path)
    b = live(FakeProvider([], model="fake-2"), fixtures_dir=tmp_path)
    assert a.request_sha256(request()) != b.request_sha256(request())
    assert a.request_sha256(request("x")) != a.request_sha256(request("y"))


@pytest.mark.parametrize(
    "body", ["Bank account: 6348 1910 2752", "acct 634819102752", "ref 1234-5678-9012"]
)
def test_account_like_digits_are_refused_before_sending(body: str) -> None:
    provider = FakeProvider([text("never")])
    with pytest.raises(SensitiveDataError):
        live(provider).generate(request(body))
    assert provider.seen == []


def test_retries_rate_limits_with_backoff_then_succeeds() -> None:
    gate = FreeGate()
    sleeps: list[float] = []
    provider = FakeProvider([rate_limited(), rate_limited(), text("ok")])
    client = LlmClient(provider, LlmMode.LIVE, gate, sleep=sleeps.append)  # type: ignore[arg-type]
    result = client.generate(request())
    assert result.response.text == "ok" and result.attempts == 3
    assert sleeps == [1.0, 2.0] and gate.taken == 3  # every attempt spends quota


def test_gives_up_after_max_attempts() -> None:
    client = live(FakeProvider([rate_limited()] * 4))
    with pytest.raises(ProviderUnavailableError):
        client.generate(request())


def test_live_mode_needs_a_quota_gate() -> None:
    client = LlmClient(FakeProvider([text("x")]), LlmMode.LIVE, None)
    with pytest.raises(ProviderUnavailableError):
        client.generate(request())


def test_structured_output_feeds_errors_back_and_never_coerces() -> None:
    provider = FakeProvider(
        [text('{"total": "2641.08"}'), text('{"total": "2641.08", "lines": 2}')]
    )
    result = live(provider).generate_structured(request(), Answer)
    assert result.value == Answer(total="2641.08", lines=2)
    assert not result.first_try_valid and len(result.calls) == 2
    retry = provider.seen[1]
    assert retry.response_schema == Answer.model_json_schema()
    assert "did not match the required JSON schema" in (retry.messages[-1].text or "")


def test_structured_output_gives_up() -> None:
    provider = FakeProvider([text("not json")] * 3)
    with pytest.raises(StructuredOutputError):
        live(provider).generate_structured(request(), Answer, max_retries=2)


def test_day_window_follows_pacific_midnight_across_dst() -> None:
    before = dt.datetime(2026, 11, 1, 6, 0, tzinfo=dt.UTC)  # 23:00 PDT on Oct 31
    start, end = day_window(before)
    assert start == dt.datetime(2026, 10, 31, 7, 0, tzinfo=dt.UTC)  # midnight PDT
    assert end == dt.datetime(2026, 11, 1, 7, 0, tzinfo=dt.UTC)
    start2, end2 = day_window(dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.UTC))  # DST ends Nov 1
    assert start2 == end and end2 == dt.datetime(2026, 11, 2, 8, 0, tzinfo=dt.UTC)  # midnight PST
    m_start, m_end = minute_window(dt.datetime(2026, 1, 1, 10, 3, 59, tzinfo=dt.UTC))
    assert (m_start.minute, m_end.minute) == (3, 4)


def test_smoke_request_is_synthetic_and_passes_the_guard() -> None:
    from opspilot.llm.client import guard_sensitive  # noqa: PLC0415
    from opspilot.llm.smoke import smoke_request  # noqa: PLC0415

    guard_sensitive(smoke_request())  # raises if any account-like digits were present
