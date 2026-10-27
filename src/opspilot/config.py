"""Environment settings, loaded once and validated at startup (fail fast).

Names match `.env.example`; secrets come only from the environment.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_MIN_SECRET_LENGTH = 32


class AppEnv(StrEnum):
    LOCAL = "local"
    TEST = "test"
    PREVIEW = "preview"


class LlmProvider(StrEnum):
    GEMINI = "gemini"
    OLLAMA = "ollama"


class LlmMode(StrEnum):
    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"


PositiveInt = Annotated[int, Field(gt=0)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore", frozen=True
    )

    app_env: AppEnv
    database_url: SecretStr
    database_url_owner: SecretStr | None = None
    database_url_baseline_ro: SecretStr | None = None
    database_url_scorer: SecretStr | None = None
    database_url_verifier: SecretStr | None = None

    session_secret: SecretStr
    bank_hmac_key: SecretStr

    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.8-flash"
    llm_provider: LlmProvider = LlmProvider.GEMINI
    # Replay is the zero-cost default: nothing reaches a provider unless asked for.
    llm_mode: LlmMode = LlmMode.REPLAY
    llm_rpm: PositiveInt = 10
    llm_rpd: PositiveInt = 1000
    llm_timeout_s: PositiveInt = 60
    llm_max_attempts: PositiveInt = 4
    ollama_url: str | None = None
    ollama_model: str | None = None

    document_store_path: str = "data/documents"
    # Pre-downloaded Docling models (the Docker image sets this; local runs download on first use).
    docling_artifacts_path: str | None = None
    upload_max_bytes: PositiveInt = 10 * 1024 * 1024
    forensics_url: str | None = None
    checkpoint_retention_days: PositiveInt = 30
    session_idle_hours: PositiveInt = 8
    demo_password: SecretStr | None = None
    log_level: str = "INFO"

    @field_validator("session_secret", "bank_hmac_key")
    @classmethod
    def _long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < _MIN_SECRET_LENGTH:
            raise ValueError(f"must be at least {_MIN_SECRET_LENGTH} characters")
        return value

    @property
    def secure_cookies(self) -> bool:
        return self.app_env is not AppEnv.LOCAL


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment
