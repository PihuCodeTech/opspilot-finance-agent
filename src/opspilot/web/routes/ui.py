"""Sign-in, sign-out and the HTML pages (`06-api.md` §4, HTML UI routes).

The pages read through the same query functions as the JSON API, so a page
never shows a case or an option the API would refuse. Their design comes in
P12; these templates are plain, accessible HTML.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import Field, SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from opspilot.cases import queries
from opspilot.cases.errors import ForbiddenError
from opspilot.config import Settings
from opspilot.domain.enums import CaseStatus, SupportingKind, UserRole
from opspilot.web.auth import (
    SESSION_COOKIE,
    CsrfUser,
    CurrentUser,
    MaybeUser,
    SignedIn,
    UnauthenticatedError,
    authenticate,
    create_session,
    end_session,
    same_origin,
)
from opspilot.web.db import transaction
from opspilot.web.errors import wants_html
from opspilot.web.ratelimit import LOGIN_PER_IP, LOGIN_PER_USER_AND_IP, RateLimiter
from opspilot.web.schemas import Body, body
from opspilot.web.theme import explicit_theme, views_3d_enabled

router = APIRouter()


class Login(Body):
    username: Annotated[str, Field(min_length=1, max_length=150)]
    password: Annotated[SecretStr, Field(min_length=1, max_length=1024)]


def _templates(request: Request) -> Jinja2Templates:
    templates: Jinja2Templates = request.app.state.templates
    return templates


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def page(
    request: Request, name: str, user: SignedIn | None, status: int = 200, **context: Any
) -> Response:
    return _templates(request).TemplateResponse(
        request,
        name,
        {
            "user": user,
            "csrf_token": user.csrf_token if user else None,
            "theme": explicit_theme(request),
            "views_3d": views_3d_enabled(request),
            **context,
        },
        status_code=status,
    )


# ---------------------------------------------------------------- sign-in


@router.get("/login")
def login_page(request: Request, user: MaybeUser) -> Response:
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", None)


@router.post("/auth/login")
async def login(request: Request, data: Annotated[Login, Depends(body(Login))]) -> Response:
    """Generic 401 on any failure: the response never says which part was wrong."""
    if not same_origin(request):
        raise ForbiddenError("Cross-site sign-in refused.", code="csrf_failed")
    client = request.client.host if request.client else "unknown"
    limiter: RateLimiter = request.app.state.limiter
    limiter.hit(
        (("login-ip", client), LOGIN_PER_IP),
        (("login-user", client, data.username.lower()), LOGIN_PER_USER_AND_IP),
    )
    settings = _settings(request)

    def sign_in() -> str | None:
        with transaction(request) as conn:
            user_id = authenticate(conn, data.username, data.password.get_secret_value())
            return create_session(conn, settings, user_id) if user_id else None

    token = await run_in_threadpool(sign_in)
    if token is None:
        if wants_html(request):
            return page(request, "login.html", None, 401, error="Wrong username or password.")
        raise UnauthenticatedError("Wrong username or password.")
    response: Response = (
        RedirectResponse("/", status_code=303) if wants_html(request) else Response(status_code=204)
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )
    return response


@router.post("/auth/logout")
def logout(request: Request, user: CsrfUser) -> Response:
    with transaction(request) as conn:
        end_session(conn, user.session_id)
    response: Response = (
        RedirectResponse("/login", status_code=303)
        if wants_html(request)
        else Response(status_code=204)
    )
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ---------------------------------------------------------------- pages


@router.get("/")
def queue_page(
    request: Request,
    user: CurrentUser,
    status: CaseStatus | None = None,
    cursor: str | None = None,
) -> Response:
    with transaction(request) as conn:
        cases = queries.list_cases(conn, user.actor, status=status, cursor=cursor)
    return page(
        request, "queue.html", user, cases=cases, selected=status, statuses=list(CaseStatus)
    )


@router.get("/approvals")
def approvals_page(
    request: Request,
    user: CurrentUser,
    held: bool = False,
    cursor: str | None = None,
) -> Response:
    with transaction(request) as conn:
        cases = queries.list_approvals(conn, user.actor, held=held, cursor=cursor)
    return page(request, "approvals.html", user, cases=cases, held=held)


@router.get("/cases/{case_id}")
def case_page(request: Request, case_id: uuid.UUID, user: CurrentUser) -> Response:
    with transaction(request) as conn:
        case = queries.case_detail(conn, user.actor, case_id)
    return page(
        request,
        "case.html",
        user,
        case=case,
        supporting_kinds=list(SupportingKind),
    )


@router.get("/uploads/new")
def upload_page(request: Request, user: CurrentUser) -> Response:
    queries.require_role(user.actor, UserRole.ANALYST)
    return page(
        request,
        "upload.html",
        user,
        max_mb=_settings(request).upload_max_bytes // (1024 * 1024),
    )
