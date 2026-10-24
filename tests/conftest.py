"""Shared test setup: a complete test environment, fresh caches, no live LLM."""

from collections.abc import Iterator

import pytest

from opspilot.config import get_settings
from opspilot.db.session import get_engine

# A port nothing listens on, so unit tests exercise the "database unavailable" path quickly.
UNREACHABLE_DB = "postgresql+psycopg://test:test@127.0.0.1:1/test?connect_timeout=1"

TEST_ENV = {
    "APP_ENV": "test",
    "DATABASE_URL": UNREACHABLE_DB,
    "SESSION_SECRET": "s" * 48,
    "BANK_HMAC_KEY": "k" * 48,
    "LLM_MODE": "replay",
}


@pytest.fixture(autouse=True)
def test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(pytest.__file__.rsplit("/", 1)[0])  # keep a developer's .env out of tests
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
