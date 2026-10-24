"""Alembic environment: online migrations only, as the owner role."""

from alembic import context
from sqlalchemy import create_engine, pool

from opspilot.config import get_settings

# Models are attached in P2; until then there is nothing to autogenerate against.
target_metadata = None


def _owner_url() -> str:
    settings = get_settings()
    url = settings.database_url_owner or settings.database_url
    return url.get_secret_value()


def run_migrations_online() -> None:
    engine = create_engine(_owner_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported; run against a database.")
run_migrations_online()
