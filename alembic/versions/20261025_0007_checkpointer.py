"""LangGraph checkpoint tables (langgraph-checkpoint-postgres 3.1.2).

The library normally creates these itself (`PostgresSaver.setup()`), but the
app role has no DDL rights, and three of its index migrations use
`CREATE INDEX CONCURRENTLY`, which can't run inside a migration transaction.
So the same DDL is applied here, without `CONCURRENTLY` (the tables are
new and empty), and the library's migration versions 0–9 are recorded so it
never needs to migrate at runtime. A test fails if a library upgrade adds a
migration (`test_checkpointer_schema_is_current`).

Checkpoints are execution state, not the record (D10); the app may delete them.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIBRARY_MIGRATIONS = 10  # len(langgraph.checkpoint.postgres.base.MIGRATIONS) at 3.1.2
TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")


def upgrade() -> None:
    op.execute("CREATE TABLE checkpoint_migrations (v INTEGER PRIMARY KEY)")
    op.execute(
        """CREATE TABLE checkpoints (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            parent_checkpoint_id TEXT,
            type TEXT,
            checkpoint JSONB NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}',
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        )"""
    )
    op.execute(
        """CREATE TABLE checkpoint_blobs (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            version TEXT NOT NULL,
            type TEXT NOT NULL,
            blob BYTEA,
            PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
        )"""
    )
    op.execute(
        """CREATE TABLE checkpoint_writes (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            channel TEXT NOT NULL,
            type TEXT,
            blob BYTEA NOT NULL,
            task_path TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        )"""
    )
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        op.execute(f"CREATE INDEX {table}_thread_id_idx ON {table}(thread_id)")
    op.execute(
        "INSERT INTO checkpoint_migrations (v) SELECT generate_series(0, :n - 1)".replace(
            ":n", str(LIBRARY_MIGRATIONS)
        )
    )
    tables = ", ".join(TABLES)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO opspilot_app")
    op.execute(f"GRANT SELECT ON {tables} TO opspilot_verifier, opspilot_scorer")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE {table}")
