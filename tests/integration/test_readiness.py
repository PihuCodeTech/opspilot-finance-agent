"""Needs a real PostgreSQL. Run with TEST_DATABASE_URL set (CI provides one)."""

import os

import pytest
from fastapi.testclient import TestClient

from opspilot.config import get_settings
from opspilot.db.session import get_engine
from opspilot.web.app import create_app

pytestmark = pytest.mark.integration

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def real_database(monkeypatch: pytest.MonkeyPatch) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    get_engine.cache_clear()


def test_readyz_is_ready_against_a_migrated_database() -> None:
    response = TestClient(create_app()).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok", "migrations": "at_head"}
