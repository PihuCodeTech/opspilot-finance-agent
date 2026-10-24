"""Enable login for the runtime roles with passwords taken from the environment.

Migrations create the roles without LOGIN or passwords. This runs as the owner
after `alembic upgrade head`, so secrets live only in `.env`, never in code.
"""

from dataclasses import dataclass

from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from opspilot.config import Settings

ROLE_URLS: dict[str, str] = {
    "opspilot_app": "database_url",
    "opspilot_baseline_ro": "database_url_baseline_ro",
    "opspilot_scorer": "database_url_scorer",
    "opspilot_verifier": "database_url_verifier",
}


class RoleProvisioningError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoleLogin:
    role: str
    password: str


def role_logins(settings: Settings) -> list[RoleLogin]:
    """Read each role's credentials from its connection URL; the user must equal the role."""
    logins: list[RoleLogin] = []
    problems: list[str] = []
    for role, field in ROLE_URLS.items():
        secret = getattr(settings, field)
        if secret is None:
            problems.append(f"{field.upper()} is not set")
            continue
        url = make_url(secret.get_secret_value())
        if url.username != role:
            problems.append(f"{field.upper()} must connect as {role}, not {url.username}")
        elif not url.password:
            problems.append(f"{field.upper()} has no password")
        else:
            logins.append(RoleLogin(role, str(url.password)))
    if problems:
        raise RoleProvisioningError("; ".join(problems))
    return logins


def provision_roles(settings: Settings) -> list[str]:
    if settings.database_url_owner is None:
        raise RoleProvisioningError("DATABASE_URL_OWNER is not set")
    logins = role_logins(settings)
    engine = create_engine(settings.database_url_owner.get_secret_value())
    raw = engine.raw_connection()
    try:
        driver = raw.driver_connection
        if driver is None:
            raise RoleProvisioningError("no driver connection available")
        with driver.cursor() as cursor:
            for login in logins:
                statement = sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(login.role), sql.Literal(login.password)
                )
                cursor.execute(statement)
        raw.commit()
    finally:
        raw.close()
        engine.dispose()
    return [login.role for login in logins]
