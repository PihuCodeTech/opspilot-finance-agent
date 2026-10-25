"""Provider-neutral request and response types (`06-architecture.md` §9).

Text only: there is deliberately no image or file part, so nothing but
preprocessed, tokenised text can ever be sent to a model (`rules.md` §11).
"""

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "model"]


@dataclass(frozen=True)
class FunctionCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class FunctionResult:
    name: str
    response: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    text: str | None = None
    function_calls: tuple[FunctionCall, ...] = ()
    function_results: tuple[FunctionResult, ...] = ()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema (from a Pydantic model)


@dataclass(frozen=True)
class LlmRequest:
    purpose: str  # extract | match | investigate | explain | baseline | smoke
    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    response_schema: dict[str, Any] | None = None  # JSON schema for structured output
    temperature: float = 0.0
    seed: int = 7
    max_output_tokens: int = 8192

    def canonical(self) -> dict[str, Any]:
        """Everything that determines the response, for hashing and replay."""
        return {
            "purpose": self.purpose,
            "system": self.system,
            "messages": [
                {
                    "role": m.role,
                    "text": m.text,
                    "function_calls": [{"name": c.name, "args": c.args} for c in m.function_calls],
                    "function_results": [
                        {"name": r.name, "response": r.response} for r in m.function_results
                    ],
                }
                for m in self.messages
            ],
            "tools": [
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in self.tools
            ],
            "response_schema": self.response_schema,
            "temperature": str(self.temperature),
            "seed": self.seed,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True)
class LlmResponse:
    text: str | None
    function_calls: tuple[FunctionCall, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "function_calls": [{"name": c.name, "args": c.args} for c in self.function_calls],
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "finish_reason": self.finish_reason,
        }

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> "LlmResponse":
        return cls(
            text=data.get("text"),
            function_calls=tuple(
                FunctionCall(c["name"], c["args"]) for c in data.get("function_calls", [])
            ),
            tokens_in=int(data.get("tokens_in", 0)),
            tokens_out=int(data.get("tokens_out", 0)),
            finish_reason=data.get("finish_reason"),
        )
