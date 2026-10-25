"""Demo users and the active policy version (`06-database.md` §9).

Idempotent: existing users are left alone (and their passwords are never
printed again); a policy version is inserted once and activated. Local and
preview environments only.
"""

import secrets
from dataclasses import dataclass
from decimal import Decimal

from argon2 import PasswordHasher
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from opspilot.db.models import PolicyChunk, PolicyVersion, User
from opspilot.domain.enums import UserRole
from opspilot.policy.chunks import split_sections
from opspilot.policy.document import render_policy
from opspilot.policy.loader import LoadedPolicy


@dataclass(frozen=True)
class DemoUser:
    username: str
    display_name: str
    role: UserRole
    approval_limit: Decimal | None = None
    approval_unlimited: bool = False


# Synthetic people; one role each, so nobody can both submit and approve (D11).
DEMO_USERS: tuple[DemoUser, ...] = (
    DemoUser("priya.raman", "Priya Raman", UserRole.ANALYST),
    DemoUser("tomas.berg", "Tomas Berg", UserRole.ANALYST),
    DemoUser("sam.okafor", "Sam Okafor", UserRole.APPROVER, approval_limit=Decimal("10000.00")),
    DemoUser("lena.hartmann", "Lena Hartmann", UserRole.APPROVER, approval_unlimited=True),
    DemoUser("ines.duarte", "Ines Duarte", UserRole.AUDITOR),
)


@dataclass(frozen=True)
class SeedResult:
    created_users: list[str]
    password: str | None  # set only when users were created with a generated password
    policy_version: str
    policy_created: bool
    chunk_count: int


def _seed_users(session: Session, password: str) -> list[str]:
    hasher = PasswordHasher()
    existing = set(session.scalars(select(User.username)))
    created = []
    for demo in DEMO_USERS:
        if demo.username in existing:
            continue
        session.add(
            User(
                username=demo.username,
                display_name=demo.display_name,
                password_hash=hasher.hash(password),
                role=demo.role,
                approval_limit=demo.approval_limit,
                approval_unlimited=demo.approval_unlimited,
            )
        )
        created.append(demo.username)
    return created


def _seed_policy(session: Session, policy: LoadedPolicy) -> tuple[bool, int]:
    current = session.scalar(select(PolicyVersion).where(PolicyVersion.version == policy.version))
    if current is not None:
        rendered = render_policy(policy)
        if (current.config_sha256, current.document_sha256) != (
            policy.config_sha256,
            rendered.document_sha256,
        ):
            raise RuntimeError(
                f"policy version {policy.version} exists with different content; bump `version`"
            )
        created = False
    else:
        document = render_policy(policy)
        current = PolicyVersion(
            version=policy.version,
            config=policy.config.model_dump(mode="json"),
            config_sha256=policy.config_sha256,
            document=document.text,
            document_sha256=document.document_sha256,
        )
        session.add(current)
        session.flush()
        for chunk in split_sections(document.text):
            session.add(
                PolicyChunk(
                    policy_version_id=current.id,
                    section_id=chunk.section_id,
                    title=chunk.title,
                    text=chunk.text,
                )
            )
        created = True
    session.execute(
        update(PolicyVersion).where(PolicyVersion.id != current.id).values(is_active=False)
    )
    session.flush()
    current.is_active = True
    count = session.scalar(select(func.count()).where(PolicyChunk.policy_version_id == current.id))
    return created, int(count or 0)


def seed(engine: Engine, policy: LoadedPolicy, demo_password: str | None) -> SeedResult:
    password = demo_password or secrets.token_urlsafe(12)
    with Session(engine) as session, session.begin():
        created_users = _seed_users(session, password)
        policy_created, chunk_count = _seed_policy(session, policy)
    return SeedResult(
        created_users=created_users,
        password=password if created_users and demo_password is None else None,
        policy_version=policy.version,
        policy_created=policy_created,
        chunk_count=chunk_count,
    )
