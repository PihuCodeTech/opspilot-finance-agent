"""Build the configured LLM client (one place reads the LLM settings)."""

from pathlib import Path

from opspilot.config import LlmMode, LlmProvider, Settings
from opspilot.db.session import get_engine
from opspilot.llm.client import LlmClient
from opspilot.llm.errors import ProviderUnavailableError
from opspilot.llm.providers import GeminiProvider, OllamaProvider, Provider
from opspilot.llm.quota import QuotaGate


class _OfflineProvider:
    """Stands in during replay: carries the provider/model identity, never calls anything."""

    def __init__(self, name: str, model: str) -> None:
        self.name, self.model = name, model

    def generate(self, request: object) -> object:
        raise ProviderUnavailableError("replay mode never calls a provider")


def build_provider(settings: Settings) -> Provider:
    if settings.llm_mode is LlmMode.REPLAY:
        model = (
            settings.gemini_model
            if settings.llm_provider is LlmProvider.GEMINI
            else settings.ollama_model or ""
        )
        return _OfflineProvider(settings.llm_provider.value, model)  # type: ignore[return-value]
    if settings.llm_provider is LlmProvider.OLLAMA:
        if not (settings.ollama_url and settings.ollama_model):
            raise ProviderUnavailableError("OLLAMA_URL and OLLAMA_MODEL must be set")
        return OllamaProvider(settings.ollama_url, settings.ollama_model, settings.llm_timeout_s)
    if settings.gemini_api_key is None:
        raise ProviderUnavailableError("GEMINI_API_KEY is not set (free tier; never link billing)")
    return GeminiProvider(
        settings.gemini_api_key.get_secret_value(), settings.gemini_model, settings.llm_timeout_s
    )


def build_client(settings: Settings, fixtures_dir: Path | None = None) -> LlmClient:
    provider = build_provider(settings)
    engine = get_engine()
    quota = None
    if settings.llm_mode is not LlmMode.REPLAY:
        quota = QuotaGate(engine, provider.name, provider.model, settings.llm_rpm, settings.llm_rpd)
    return LlmClient(
        provider, settings.llm_mode, quota, engine, fixtures_dir, settings.llm_max_attempts
    )
