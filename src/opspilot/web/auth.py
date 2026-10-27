"""Sessions, login and CSRF (`06-architecture.md` §7, `06-api.md` §2).

- The session cookie holds a random token. The database keeps only its
  HMAC-SHA256 under `SESSION_SECRET`, so a leaked `sessions` table can't be
  replayed as cookies.
- Sessions expire after `SESSION_IDLE_HOURS` without a request; each request
  slides the expiry (written at most once a minute).
- Each session has its own CSRF token, required on every non-GET request
  as the `X-CSRF-Token` header or the `csrf_token` form field.
- Passwords are argon2id. An unknown username is verified against a dummy
  hash, so the response time doesn't reveal which usernames exist.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from functools import cache
from typing import Annotated, Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends
from sqlalchemy import Connection, delete, func, select, text, update
from starlette.datastructures import FormData
from starlette.requests import Request

from opspilot.cases.errors import ForbiddenError, ServiceError
from opspilot.cases.state import Actor
from opspilot.config import Settings
from opspilot.db.models import Session, User
from opspilot.domain.enums import UserRole
from opspilot.web.db import transaction

SESSION_COOKIE = "opspilot_session"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TOUCH_AFTER_SECONDS = 60
# Limits for every form this app accepts; the largest is one upload plus three fields.
FORM_MAX_FILES = 1
FORM_MAX_FIELDS = 64


class UnauthenticatedError(ServiceError):
    status = 401
    code = "unauthenticated"

    def __init__(self, message: str = "Sign in to continue.") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class SignedIn:
    actor: Actor
    session_id: uuid.UUID
    csrf_token: str
    username: str
    display_name: str

    @property
    def role(self) -> UserRole:
        return self.actor.role


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def token_hash(settings: Settings, token: str) -> bytes:
    key = settings.session_secret.get_secret_value().encode()
    return hmac.new(key, token.encode(), hashlib.sha256).digest()


@cache
def _hasher() -> PasswordHasher:
    return PasswordHasher()


@cache
def _dummy_hash() -> str:
    return _hasher().hash(secrets.token_urlsafe(16))


def authenticate(conn: Connection, username: str, password: str) -> uuid.UUID | None:
    """The user's ID when the credentials are right and the account is active."""
    user = conn.execute(
        select(User.id, User.password_hash, User.is_active).where(User.username == username)
    ).first()
    stored = user.password_hash if user is not None else _dummy_hash()
    try:
        _hasher().verify(stored, password)
    except (VerificationError, InvalidHashError):
        return None
    if user is None or not user.is_active:
        return None
    return uuid.UUID(str(user.id))


def create_session(conn: Connection, settings: Settings, user_id: uuid.UUID) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        text(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, last_seen_at, expires_at)"
            " VALUES (:h, :u, :c, now(), now() + make_interval(hours => :idle))"
        ),
        {
            "h": token_hash(settings, token),
            "u": user_id,
            "c": secrets.token_urlsafe(32),
            "idle": settings.session_idle_hours,
        },
    )
    return token


def end_session(conn: Connection, session_id: uuid.UUID) -> None:
    conn.execute(delete(Session).where(Session.id == session_id))


def _seconds(seconds: int) -> Any:
    return func.make_interval(0, 0, 0, 0, 0, 0, seconds)


def load_session(conn: Connection, settings: Settings, token: str) -> SignedIn | None:
    row = conn.execute(
        select(
            Session.id,
            Session.csrf_token,
            Session.last_seen_at,
            (Session.expires_at > func.now()).label("live"),
            (Session.last_seen_at < func.now() - _seconds(TOUCH_AFTER_SECONDS)).label("stale"),
            User.id.label("user_id"),
            User.username,
            User.display_name,
            User.role,
            User.approval_limit,
            User.approval_unlimited,
            User.is_active,
        )
        .join(User, User.id == Session.user_id)
        .where(Session.token_hash == token_hash(settings, token))
    ).first()
    if row is None:
        return None
    if not row.live or not row.is_active:
        end_session(conn, row.id)
        return None
    if row.stale:
        conn.execute(
            update(Session)
            .where(Session.id == row.id)
            .values(
                last_seen_at=func.now(),
                expires_at=func.now() + _seconds(settings.session_idle_hours * 3600),
            )
        )
    actor = Actor(row.user_id, UserRole(row.role), row.approval_limit, row.approval_unlimited)
    return SignedIn(actor, row.id, row.csrf_token, row.username, row.display_name)


def optional_user(request: Request) -> SignedIn | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with transaction(request) as conn:
        return load_session(conn, _settings(request), token)


MaybeUser = Annotated[SignedIn | None, Depends(optional_user)]


def signed_in(user: MaybeUser) -> SignedIn:
    if user is None:
        raise UnauthenticatedError
    return user


CurrentUser = Annotated[SignedIn, Depends(signed_in)]


async def read_form(request: Request) -> FormData:
    """The request's form, parsed once with this app's limits (Starlette caches it)."""
    return await request.form(max_files=FORM_MAX_FILES, max_fields=FORM_MAX_FIELDS)


def is_form(request: Request) -> bool:
    kind = request.headers.get("content-type", "")
    return kind.startswith(("application/x-www-form-urlencoded", "multipart/form-data"))


async def csrf_protected(request: Request, user: CurrentUser) -> SignedIn:
    """The signed-in user, after checking the CSRF token on any non-GET request."""
    if request.method in SAFE_METHODS:
        return user
    given = request.headers.get(CSRF_HEADER)
    if given is None and is_form(request):
        value = (await read_form(request)).get(CSRF_FIELD)
        given = value if isinstance(value, str) else None
    if not given or not hmac.compare_digest(given, user.csrf_token):
        raise ForbiddenError("The form expired. Reload the page and try again.", code="csrf_failed")
    return user


def same_origin(request: Request) -> bool:
    """For the login form, which has no session (and so no CSRF token) yet."""
    origin = request.headers.get("origin")
    if origin is None or origin == "null":
        return origin is None
    return origin == f"{request.url.scheme}://{request.url.netloc}"


CsrfUser = Annotated[SignedIn, Depends(csrf_protected)]
