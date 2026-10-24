"""Migrations apply, roll back and re-apply cleanly, and match the models exactly."""

from collections.abc import Iterator

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from opspilot.db.models import Base
from tests.integration.conftest import (
    _require_db,
    create_database,
    downgrade,
    drop_database,
    upgrade,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def fresh_url() -> Iterator[str]:
    base = _require_db()
    url = create_database(base)
    try:
        yield url
    finally:
        drop_database(base, url)


def test_upgrade_downgrade_upgrade(fresh_url: str) -> None:
    upgrade(fresh_url)
    downgrade(fresh_url, "base")
    engine = create_engine(fresh_url)
    with engine.connect() as conn:
        leftover = conn.execute(
            text(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
                " AND tablename <> 'alembic_version'"
            )
        ).scalar_one()
        types = conn.execute(
            text(
                "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
                " WHERE n.nspname = 'public' AND t.typtype = 'e'"
            )
        ).scalar_one()
    engine.dispose()
    assert (leftover, types) == (0, 0)
    upgrade(fresh_url)


def test_models_and_migrations_do_not_drift(migrated_url: str) -> None:
    engine = create_engine(migrated_url)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_server_default": True})
        diff = compare_metadata(context, Base.metadata)
    engine.dispose()
    assert diff == []
