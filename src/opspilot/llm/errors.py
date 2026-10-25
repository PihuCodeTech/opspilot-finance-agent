"""LLM failures. Each one leads to a safe, visible state, never a silent wrong answer."""

import datetime as dt


class LlmError(RuntimeError):
    pass


class QuotaExhaustedError(LlmError):
    """The daily free-tier quota is used up: pause until `reset_at`, never retry in a loop."""

    def __init__(self, reset_at: dt.datetime) -> None:
        super().__init__(f"daily LLM quota exhausted; resets at {reset_at.isoformat()}")
        self.reset_at = reset_at


class RetryableProviderError(LlmError):
    """Rate limited (429) or a server error (5xx): worth a bounded, backed-off retry."""


class ProviderUnavailableError(LlmError):
    """Retries used up, or a non-retryable provider error: the run fails, retryable later."""


class ReplayMissError(LlmError):
    """Replay mode found no recorded response: tests must never fall through to a live call."""


class SensitiveDataError(LlmError):
    """A request still contains an account-like digit run: refused before sending."""


class StructuredOutputError(LlmError):
    """The model kept returning output that fails schema validation."""


class ProviderMixingError(LlmError):
    """One evaluation run must never mix providers or models."""
