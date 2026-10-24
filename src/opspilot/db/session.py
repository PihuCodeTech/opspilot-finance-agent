"""Engine creation and the readiness check.

P0 uses one connection (DATABASE_URL). The per-role connections from
`06-database.md` §7 are added with the schema in P2.
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
