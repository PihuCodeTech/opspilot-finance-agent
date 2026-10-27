"""A scripted provider for tests: no network, ever."""

from collections.abc import Callable
from dataclasses import dataclass, field

from opspilot.llm.errors import RetryableProviderError
from opspilot.llm.types import LlmRequest, LlmResponse

Step = LlmResponse | Exception | Callable[[LlmRequest], LlmResponse]


@dataclass
class FakeProvider:
    script: list[Step]
    name: str = "fake"
    model: str = "fake-1"
    seen: list[LlmRequest] = field(default_factory=list)

    def generate(self, request: LlmRequest) -> LlmResponse:
        self.seen.append(request)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(request)
        return step


def text(value: str, tokens: int = 10) -> LlmResponse:
    return LlmResponse(text=value, tokens_in=tokens, tokens_out=tokens)


def rate_limited() -> RetryableProviderError:
    return RetryableProviderError("fake 429")
