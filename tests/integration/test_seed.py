import pytest
from argon2 import PasswordHasher
from sqlalchemy import Engine, text

from opspilot.db.seed import DEMO_USERS, seed
from opspilot.policy.loader import load_policy

pytestmark = pytest.mark.integration


def test_seed_is_idempotent_and_activates_one_policy(owner_engine: Engine) -> None:
    policy = load_policy()
    first = seed(owner_engine, policy, demo_password="local-demo-password")
    second = seed(owner_engine, policy, demo_password="local-demo-password")

    assert set(first.created_users) <= {u.username for u in DEMO_USERS}
    assert second.created_users == []
    assert first.password is None  # an explicit DEMO_PASSWORD is never echoed back
    assert second.chunk_count > 10

    with owner_engine.connect() as conn:
        roles: dict[str, str] = {
            row[0]: row[1] for row in conn.execute(text("SELECT username, role::text FROM users"))
        }
        active = (
            conn.execute(text("SELECT version FROM policy_versions WHERE is_active"))
            .scalars()
            .all()
        )
        stored_hash = conn.execute(
            text("SELECT password_hash FROM users WHERE username = 'sam.okafor'")
        ).scalar_one()
    assert {u.username: u.role.value for u in DEMO_USERS}.items() <= roles.items()
    assert active == [policy.version]
    assert PasswordHasher().verify(stored_hash, "local-demo-password")
