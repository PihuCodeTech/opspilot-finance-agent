"""Quota enforcement and call recording against a real Postgres."""

import datetime as dt
import threading
import uuid

import pytest
from sqlalchemy import Engine, text

from opspilot.config import LlmMode
from opspilot.llm.client import LlmClient, assert_single_provider
from opspilot.llm.errors import ProviderMixingError, QuotaExhaustedError
from opspilot.llm.quota import QuotaGate
from opspilot.llm.types import LlmRequest, Message
from tests.llm_fakes import FakeProvider
from tests.llm_fakes import text as reply

pytestmark = pytest.mark.integration


class FakeClock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 20, 17, 0, 5, tzinfo=dt.UTC)
        self.slept = 0.0

    def __call__(self) -> dt.datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += dt.timedelta(seconds=seconds)


def gate(engine: Engine, clock: FakeClock, model: str, rpm: int = 3, rpd: int = 5) -> QuotaGate:
    return QuotaGate(engine, "fake", model, rpm, rpd, clock=clock, sleep=clock.sleep)


def test_minute_limit_waits_and_day_limit_stops(owner_engine: Engine) -> None:
    clock, model = FakeClock(), f"m-{uuid.uuid4().hex[:6]}"
    g = gate(owner_engine, clock, model)
    waits = [g.acquire() for _ in range(5)]
    assert waits[:3] == [0, 0, 0] and waits[3] > 0  # 4th waits for the next minute
    assert clock.slept >= 55
    with pytest.raises(QuotaExhaustedError) as info:
        g.acquire()  # 6th of a 5-per-day limit
    assert info.value.reset_at == dt.datetime(2026, 10, 21, 7, 0, tzinfo=dt.UTC)  # midnight PDT
    assert g.used_today() == 5


def test_counts_survive_a_restart(owner_engine: Engine) -> None:
    clock, model = FakeClock(), f"m-{uuid.uuid4().hex[:6]}"
    for _ in range(3):
        gate(owner_engine, clock, model, rpm=10).acquire()
    restarted = gate(owner_engine, clock, model, rpm=10)  # a new process, same database
    assert restarted.used_today() == 3
    restarted.acquire()
    restarted.acquire()
    with pytest.raises(QuotaExhaustedError):
        restarted.acquire()


def test_concurrent_workers_never_exceed_the_day_limit(owner_engine: Engine) -> None:
    model = f"m-{uuid.uuid4().hex[:6]}"
    granted, refused = [], []
    lock = threading.Lock()

    def worker() -> None:
        g = QuotaGate(owner_engine, "fake", model, 1000, 7, sleep=lambda _s: None)
        try:
            g.acquire()
            with lock:
                granted.append(1)
        except QuotaExhaustedError:
            with lock:
                refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (len(granted), len(refused)) == (7, 13)


def test_live_calls_are_recorded_and_replayable(owner_engine: Engine) -> None:
    clock, model = FakeClock(), f"m-{uuid.uuid4().hex[:6]}"
    provider = FakeProvider([reply("recorded answer")], model=model)
    live = LlmClient(provider, LlmMode.LIVE, gate(owner_engine, clock, model, rpd=50), owner_engine)
    req = LlmRequest("smoke", "sys", (Message("user", text=f"q {uuid.uuid4().hex}"),))
    result = live.generate(req)

    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT provider, model, status, request FROM llm_calls WHERE request_sha256 = :h"
            ),
            {"h": result.request_sha256},
        ).one()
    assert (row.provider, row.model, row.status) == ("fake", model, "ok")

    replay = LlmClient(FakeProvider([], model=model), LlmMode.REPLAY, None, owner_engine)
    assert (
        replay.generate(req).response.text == "recorded answer"
    )  # from llm_calls, no provider call


def test_an_evaluation_run_cannot_mix_providers(owner_engine: Engine) -> None:
    eval_run = uuid.uuid4()
    with owner_engine.begin() as c:
        ds = c.execute(
            text(
                "INSERT INTO datasets (name, version, split) VALUES ('mix', :v, 'dev') RETURNING id"
            ),
            {"v": eval_run.hex[:8]},
        ).scalar_one()
        pv = c.execute(
            text(
                "INSERT INTO policy_versions (version, config, config_sha256, document,"
                " document_sha256) VALUES (:v, '{}'::jsonb, :h, 'd', :h) RETURNING id"
            ),
            {"v": f"mix-{eval_run.hex[:8]}", "h": "0" * 64},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO eval_runs (id, system, dataset_id, provider, model, prompt_versions,"
                " policy_version_id, git_sha, status) VALUES (:id, 'opspilot', :d, 'fake',"
                " 'fake-1', '{}'::jsonb, :p, 'abc', 'running')"
            ),
            {"id": eval_run, "d": ds, "p": pv},
        )
        c.execute(
            text(
                "INSERT INTO llm_calls (eval_run_id, purpose, provider, model, request_sha256,"
                " request, status) VALUES (:e, 'x', 'fake', 'fake-1', :h, '{}'::jsonb, 'ok')"
            ),
            {"e": eval_run, "h": "1" * 64},
        )
    assert_single_provider(owner_engine, eval_run, "fake", "fake-1")
    with pytest.raises(ProviderMixingError):
        assert_single_provider(owner_engine, eval_run, "gemini", "gemini-3.8-flash")
