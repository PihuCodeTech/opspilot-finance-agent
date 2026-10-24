import pytest
from pydantic import ValidationError

from opspilot.config import AppEnv, LlmMode, Settings


def _settings() -> Settings:
    return Settings()


def test_loads_required_values() -> None:
    settings = _settings()
    assert settings.app_env is AppEnv.TEST
    assert settings.secure_cookies


def test_llm_defaults_to_replay_so_nothing_is_charged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODE")
    assert _settings().llm_mode is LlmMode.REPLAY


@pytest.mark.parametrize("name", ["APP_ENV", "DATABASE_URL", "SESSION_SECRET", "BANK_HMAC_KEY"])
def test_missing_required_value_fails_fast(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(ValidationError):
        _settings()


@pytest.mark.parametrize("name", ["SESSION_SECRET", "BANK_HMAC_KEY"])
def test_short_secrets_are_rejected(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "too-short")
    with pytest.raises(ValidationError):
        _settings()


def test_empty_optional_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_RPM", "")
    monkeypatch.setenv("GEMINI_MODEL", "")
    settings = _settings()
    assert settings.llm_rpm == 10
    assert settings.gemini_model == "gemini-3.8-flash"


def test_secrets_are_not_printed() -> None:
    assert "s" * 48 not in repr(_settings())
