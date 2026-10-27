"""The one way the rest of the system calls a model (`rules.md` §11).

Every request is checked for account-like digits and refused if any remain,
hashed, then either replayed from a recording (`replay`: tests and rescoring;
a miss is an error, never a live call) or sent live through the quota gate
with bounded, backed-off retries (`live`, `record`). Live calls are recorded
in `llm_calls`; `record` also writes a fixture file for tests.
"""

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, insert, select

from opspilot.config import LlmMode
from opspilot.db.models import LlmCall
from opspilot.domain.digest import canonical_json, sha256_hex
from opspilot.extraction.bank_tokens import contains_account_like_digits
from opspilot.llm.errors import (
    ProviderMixingError,
    ProviderUnavailableError,
    ReplayMissError,
    RetryableProviderError,
    SensitiveDataError,
    StructuredOutputError,
)
from opspilot.llm.providers import Provider
from opspilot.llm.quota import QuotaGate
from opspilot.llm.types import LlmRequest, LlmResponse, Message

log = structlog.get_logger()
ModelT = TypeVar("ModelT", bound=BaseModel)
BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0)


@dataclass(frozen=True)
class LlmResult:
    response: LlmResponse
    request_sha256: str
    latency_ms: int
    quota_wait_ms: int
    attempts: int
    replayed: bool


@dataclass(frozen=True)
class StructuredResult[ModelT: BaseModel]:
    value: ModelT
    calls: tuple[LlmResult, ...]  # every call made, including rejected tries
    first_try_valid: bool


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def guard_sensitive(request: LlmRequest) -> None:
    for text in _strings(request.canonical()):
        if contains_account_like_digits(text):
            raise SensitiveDataError("request contains an account-like digit run; refused")


@dataclass
class LlmClient:
    provider: Provider
    mode: LlmMode
    quota: QuotaGate | None  # required for live and record modes
    engine: Engine | None = None  # where live calls are recorded (llm_calls)
    fixtures_dir: Path | None = None  # replay source / record target for tests
    max_attempts: int = 4
    sleep: Callable[[float], None] = time.sleep
    _replay_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------- recordings

    def _fixture_path(self, request_sha: str) -> Path | None:
        return self.fixtures_dir / f"{request_sha}.json" if self.fixtures_dir else None

    def _replay(self, request_sha: str) -> LlmResponse:
        path = self._fixture_path(request_sha)
        if path is not None and path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            return LlmResponse.from_record(record["response"])
        if self.engine is not None:
            with self.engine.connect() as conn:
                stored = conn.execute(
                    select(LlmCall.response)
                    .where(LlmCall.request_sha256 == request_sha, LlmCall.status == "ok")
                    .order_by(LlmCall.created_at.desc())
                    .limit(1)
                ).scalar()
            if stored is not None:
                return LlmResponse.from_record(stored)
        raise ReplayMissError(f"no recorded response for request {request_sha[:12]}")

    def _record(
        self,
        request: LlmRequest,
        request_sha: str,
        result: LlmResult,
        *,
        status: str,
        run_id: uuid.UUID | None,
        eval_run_id: uuid.UUID | None,
    ) -> None:
        if self.engine is not None:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(LlmCall).values(
                        run_id=run_id,
                        eval_run_id=eval_run_id,
                        purpose=request.purpose,
                        provider=self.provider.name,
                        model=self.provider.model,
                        request_sha256=request_sha,
                        request=request.canonical(),
                        response=result.response.to_record() if status == "ok" else None,
                        status=status,
                        tokens_in=result.response.tokens_in,
                        tokens_out=result.response.tokens_out,
                        latency_ms=result.latency_ms,
                        quota_wait_ms=result.quota_wait_ms,
                    )
                )
        if (
            self.mode is LlmMode.RECORD
            and status == "ok"
            and (path := self._fixture_path(request_sha))
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                canonical_json(
                    {
                        "request_sha256": request_sha,
                        "provider": self.provider.name,
                        "model": self.provider.model,
                        "request": request.canonical(),
                        "response": result.response.to_record(),
                    }
                )
            )

    # ------------------------------------------------------------- calls

    def request_sha256(self, request: LlmRequest) -> str:
        return sha256_hex(
            {
                "provider": self.provider.name,
                "model": self.provider.model,
                "request": request.canonical(),
            }
        )

    def generate(
        self,
        request: LlmRequest,
        *,
        run_id: uuid.UUID | None = None,
        eval_run_id: uuid.UUID | None = None,
    ) -> LlmResult:
        guard_sensitive(request)
        request_sha = self.request_sha256(request)
        if self.mode is LlmMode.REPLAY:
            return LlmResult(self._replay(request_sha), request_sha, 0, 0, 0, replayed=True)
        if self.quota is None:
            raise ProviderUnavailableError("live calls need a quota gate")
        quota_wait_ms = 0
        for attempt in range(1, self.max_attempts + 1):
            quota_wait_ms += (
                self.quota.acquire()
            )  # every attempt spends quota; raises when the day is full
            started = time.monotonic()
            try:
                response = self.provider.generate(request)
            except RetryableProviderError as exc:
                if attempt == self.max_attempts:
                    raise ProviderUnavailableError(
                        f"gave up after {attempt} attempts: {exc}"
                    ) from exc
                pause = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                log.warning("llm.retry", attempt=attempt, pause_s=pause, reason=str(exc))
                self.sleep(pause)
                quota_wait_ms += int(pause * 1000)
                continue
            result = LlmResult(
                response,
                request_sha,
                int((time.monotonic() - started) * 1000),
                quota_wait_ms,
                attempt,
                replayed=False,
            )
            self._record(
                request, request_sha, result, status="ok", run_id=run_id, eval_run_id=eval_run_id
            )
            return result
        raise ProviderUnavailableError("unreachable")  # pragma: no cover

    def generate_structured(
        self,
        request: LlmRequest,
        model: type[ModelT],
        *,
        max_retries: int = 2,
        run_id: uuid.UUID | None = None,
        eval_run_id: uuid.UUID | None = None,
    ) -> StructuredResult[ModelT]:
        """Ask for JSON matching `model`; invalid output goes back with the error, never coerced."""
        request = replace(request, response_schema=model.model_json_schema())
        calls: list[LlmResult] = []
        for attempt in range(max_retries + 1):
            result = self.generate(request, run_id=run_id, eval_run_id=eval_run_id)
            calls.append(result)
            raw = result.response.text or ""
            try:
                value = model.model_validate_json(raw)
            except ValidationError as exc:
                if attempt == max_retries:
                    raise StructuredOutputError(
                        f"invalid after {attempt + 1} tries: {exc}"
                    ) from exc
                feedback = (
                    "Your previous reply did not match the required JSON schema. Errors:\n"
                    + exc.json(include_url=False, include_input=False)
                    + "\nReply again with JSON that matches the schema exactly."
                )
                request = replace(
                    request,
                    messages=(
                        *request.messages,
                        Message("model", text=raw),
                        Message("user", text=feedback),
                    ),
                )
                continue
            return StructuredResult(value, tuple(calls), first_try_valid=attempt == 0)
        raise StructuredOutputError("unreachable")  # pragma: no cover


def assert_single_provider(
    engine: Engine, eval_run_id: uuid.UUID, provider: str, model: str
) -> None:
    """An evaluation run must use one provider and model throughout (P6, `rules.md` §11)."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(LlmCall.provider, LlmCall.model)
            .where(LlmCall.eval_run_id == eval_run_id)
            .distinct()
        )
        used = {(row[0], row[1]) for row in rows}
    if used - {(provider, model)}:
        raise ProviderMixingError(f"evaluation run {eval_run_id} already used {sorted(used)}")
