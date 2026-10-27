"""Integration fixtures: a freshly migrated temporary database per test session.

Needs TEST_DATABASE_URL pointing at a PostgreSQL server as a role that can create
databases and roles (the Compose owner, or CI's service user). Each test runs in
a transaction that is rolled back, so append-only rows never leak between tests.
"""

import os
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

from opspilot.config import get_settings
from opspilot.db.session import _ALEMBIC_INI, get_engine, get_owner_engine

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _require_db() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    return TEST_DATABASE_URL


def _admin_engine(url: str) -> Engine:
    return create_engine(url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)


def create_database(base_url: str) -> str:
    name = f"opspilot_test_{uuid.uuid4().hex[:12]}"
    admin = _admin_engine(base_url)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return make_url(base_url).set(database=name).render_as_string(hide_password=False)


def drop_database(base_url: str, url: str) -> None:
    name = make_url(url).database
    admin = _admin_engine(base_url)
    with admin.connect() as conn:
        conn.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
            {"n": name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    admin.dispose()


def point_settings_at(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_URL_OWNER", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_owner_engine.cache_clear()


def upgrade(url: str, revision: str = "head") -> None:
    with pytest.MonkeyPatch.context() as mp:
        point_settings_at(mp, url)
        command.upgrade(Config(str(_ALEMBIC_INI)), revision)


def downgrade(url: str, revision: str) -> None:
    with pytest.MonkeyPatch.context() as mp:
        point_settings_at(mp, url)
        command.downgrade(Config(str(_ALEMBIC_INI)), revision)


@pytest.fixture(scope="session")
def migrated_url() -> Iterator[str]:
    base = _require_db()
    url = create_database(base)
    try:
        upgrade(url)
        yield url
    finally:
        drop_database(base, url)


@pytest.fixture
def owner_engine(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    point_settings_at(monkeypatch, migrated_url)
    engine = create_engine(migrated_url)
    yield engine
    engine.dispose()


@pytest.fixture
def conn(owner_engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is always rolled back."""
    with owner_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()
