"""Model providers behind one interface: Gemini (free tier) and a local Ollama fallback.

Only this module talks to a provider SDK or endpoint. Providers raise
`RetryableProviderError` for rate limits and server errors and
`ProviderUnavailableError` for everything else; the client decides on retries.
"""

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from opspilot.llm.errors import ProviderUnavailableError, RetryableProviderError
from opspilot.llm.types import FunctionCall, LlmRequest, LlmResponse

HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500


class Provider(Protocol):
    name: str
    model: str

    def generate(self, request: LlmRequest) -> LlmResponse: ...


class GeminiProvider:
    """Gemini API via the official `google-genai` SDK. Free tier only: never link billing."""

    name = "gemini"

    def __init__(self, api_key: str, model: str, timeout_s: int = 60) -> None:
        from google import genai  # noqa: PLC0415  # imported only when the provider is used
        from google.genai import types  # noqa: PLC0415

        self.model = model
        self._types = types
        self._client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=timeout_s * 1000)
        )

    def _contents(self, request: LlmRequest) -> list[Any]:
        t = self._types
        contents = []
        for message in request.messages:
            parts: list[Any] = []
            if message.text:
                parts.append(t.Part.from_text(text=message.text))
            parts += [
                t.Part.from_function_call(name=c.name, args=c.args) for c in message.function_calls
            ]
            parts += [
                t.Part.from_function_response(name=r.name, response=r.response)
                for r in message.function_results
            ]
            contents.append(t.Content(role=message.role, parts=parts))
        return contents

    def generate(self, request: LlmRequest) -> LlmResponse:
        from google.genai import errors  # noqa: PLC0415

        t = self._types
        config = t.GenerateContentConfig(
            system_instruction=request.system,
            temperature=request.temperature,
            seed=request.seed,
            max_output_tokens=request.max_output_tokens,
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
        )
        if request.tools:
            config.tools = [
                t.Tool(
                    function_declarations=[
                        t.FunctionDeclaration(
                            name=s.name,
                            description=s.description,
                            parameters_json_schema=s.parameters,
                        )
                        for s in request.tools
                    ]
                )
            ]
        if request.response_schema is not None:
            config.response_mime_type = "application/json"
            config.response_json_schema = request.response_schema
        try:
            response = self._client.models.generate_content(
                model=self.model, contents=self._contents(request), config=config
            )
        except errors.APIError as exc:
            if exc.code == HTTP_TOO_MANY_REQUESTS or (exc.code or 0) >= HTTP_SERVER_ERROR:
                raise RetryableProviderError(f"gemini {exc.code} {exc.status}") from exc
            raise ProviderUnavailableError(f"gemini {exc.code} {exc.status}") from exc
        usage = response.usage_metadata
        calls = tuple(
            FunctionCall(c.name or "", dict(c.args or {})) for c in (response.function_calls or [])
        )
        finish = None
        if response.candidates and response.candidates[0].finish_reason is not None:
            finish = str(response.candidates[0].finish_reason.value)
        return LlmResponse(
            text=response.text if not calls else None,
            function_calls=calls,
            tokens_in=(usage.prompt_token_count or 0) if usage else 0,
            tokens_out=(usage.candidates_token_count or 0) if usage else 0,
            finish_reason=finish,
        )


class OllamaProvider:
    """A local model for development when the quota is spent. Never mixed with Gemini results."""

    name = "ollama"

    def __init__(self, url: str, model: str, timeout_s: int = 120) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def generate(self, request: LlmRequest) -> LlmResponse:
        messages = [{"role": "system", "content": request.system}]
        for message in request.messages:
            text = message.text or ""
            for result in message.function_results:
                text += f"\n[tool {result.name} result] {json.dumps(result.response)}"
            messages.append(
                {"role": "assistant" if message.role == "model" else "user", "content": text}
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": request.temperature, "seed": request.seed},
        }
        if request.response_schema is not None:
            body["format"] = request.response_schema
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": s.name,
                        "description": s.description,
                        "parameters": s.parameters,
                    },
                }
                for s in request.tools
            ]
        http_request = urllib.request.Request(  # noqa: S310  # URL comes from configuration
            f"{self.url}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_s) as reply:  # noqa: S310
                data = json.loads(reply.read())
        except urllib.error.HTTPError as exc:
            if exc.code == HTTP_TOO_MANY_REQUESTS or exc.code >= HTTP_SERVER_ERROR:
                raise RetryableProviderError(f"ollama {exc.code}") from exc
            raise ProviderUnavailableError(f"ollama {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RetryableProviderError(f"ollama unreachable: {exc}") from exc
        message = data.get("message", {})
        calls = tuple(
            FunctionCall(c["function"]["name"], dict(c["function"].get("arguments") or {}))
            for c in message.get("tool_calls") or []
        )
        return LlmResponse(
            text=message.get("content") if not calls else None,
            function_calls=calls,
            tokens_in=int(data.get("prompt_eval_count", 0)),
            tokens_out=int(data.get("eval_count", 0)),
            finish_reason=data.get("done_reason"),
        )
