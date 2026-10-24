"""/readyz against a real, migrated PostgreSQL."""

import pytest
from fastapi.testclient import TestClient

from opspilot.web.app import create_app
from tests.integration.conftest import point_settings_at

pytestmark = pytest.mark.integration


def test_readyz_is_ready_against_a_migrated_database(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    point_settings_at(monkeypatch, migrated_url)
    response = TestClient(create_app()).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok", "migrations": "at_head"}
