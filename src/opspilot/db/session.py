"""Engine creation and the readiness check.

Each runtime role has its own connection URL (`06-database.md` §7); code uses
the engine for the job it is doing, never a more privileged one.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text

from opspilot.config import get_settings

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = get_settings().database_url.get_secret_value()
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


@lru_cache(maxsize=1)
def get_owner_engine() -> Engine:
    """Schema owner: migrations, role provisioning and seeding only."""
    url = get_settings().database_url_owner
    if url is None:
        raise RuntimeError("DATABASE_URL_OWNER is not set")
    return create_engine(url.get_secret_value(), pool_pre_ping=True, pool_size=1, max_overflow=1)


@lru_cache(maxsize=1)
def get_scorer_engine() -> Engine:
    """Dataset generation/loading and scoring: the only runtime role that reads labels."""
    url = get_settings().database_url_scorer
    if url is None:
        raise RuntimeError("DATABASE_URL_SCORER is not set")
    return create_engine(url.get_secret_value(), pool_pre_ping=True, pool_size=1, max_overflow=1)


@lru_cache(maxsize=1)
def get_baseline_engine() -> Engine:
    """The baseline's SQL tool only: SELECT on the ERP views, 5 s statement timeout."""
    url = get_settings().database_url_baseline_ro
    if url is None:
        raise RuntimeError("DATABASE_URL_BASELINE_RO is not set")
    return create_engine(url.get_secret_value(), pool_pre_ping=True, pool_size=1, max_overflow=1)


@lru_cache(maxsize=1)
def get_verifier_engine() -> Engine:
    """Read-only role for `audit verify`: recomputes chains and digests, can change nothing."""
    url = get_settings().database_url_verifier
    if url is None:
        raise RuntimeError("DATABASE_URL_VERIFIER is not set")
    return create_engine(url.get_secret_value(), pool_pre_ping=True, pool_size=1, max_overflow=1)


@dataclass(frozen=True)
class Readiness:
    database_ok: bool
    migrations_at_head: bool
    detail: str | None = None

    @property
    def ready(self) -> bool:
        return self.database_ok and self.migrations_at_head


def _expected_heads() -> set[str]:
    script = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI)))
    return set(script.get_heads())


def check_readiness(engine: Engine) -> Readiness:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current = set(MigrationContext.configure(connection).get_current_heads())
    except Exception as exc:  # noqa: BLE001  # any connection failure means "not ready"
        return Readiness(database_ok=False, migrations_at_head=False, detail=type(exc).__name__)
    expected = _expected_heads()
    if current != expected:
        return Readiness(database_ok=True, migrations_at_head=False, detail="migrations_pending")
    return Readiness(database_ok=True, migrations_at_head=True)
