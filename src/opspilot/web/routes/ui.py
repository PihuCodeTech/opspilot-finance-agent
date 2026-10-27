"""Sign-in, sign-out and the HTML pages (`06-api.md` §4, HTML UI routes).

The pages read through the same query functions as the JSON API, so a page
never shows a case or an option the API would refuse. Their design comes in
P12; these templates are plain, accessible HTML.
"""

import uuid
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import Field, SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from opspilot.cases import queries
from opspilot.cases.errors import ForbiddenError
from opspilot.config import AppEnv, Settings
from opspilot.db.seed import DEMO_USERS
from opspilot.domain.enums import CaseStatus, FindingCode, SupportingKind, UserRole
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
from opspilot.web.present import ERROR_WORDS
from opspilot.web.ratelimit import LOGIN_PER_IP, LOGIN_PER_USER_AND_IP, RateLimiter
from opspilot.web.scenes import (
    audit_chain,
    desk_approvals,
    desk_queue,
    document_fan,
    hero,
    match_stack,
    pipeline_live,
    pipeline_track,
    resolution_chain,
)
from opspilot.web.schemas import Body, body
from opspilot.web.theme import (
    THEME_COOKIE,
    VIEWS_3D_COOKIE,
    explicit_theme,
    views_3d_enabled,
    views_3d_explicit,
)

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
            "views_3d_explicit": views_3d_explicit(request),
            **context,
        },
        status_code=status,
    )


# ---------------------------------------------------------------- sign-in


def _demo_users(request: Request) -> list[Any]:
    """The seeded demo accounts (names and roles, never passwords), local and preview only."""
    if _settings(request).app_env not in {AppEnv.LOCAL, AppEnv.PREVIEW}:
        return []
    return list(DEMO_USERS)


@router.get("/login")
def login_page(request: Request, user: MaybeUser) -> Response:
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", None, demo_users=_demo_users(request), hero=hero())


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
            return page(
                request,
                "login.html",
                None,
                401,
                error="Wrong username or password.",
                demo_users=_demo_users(request),
                hero=hero(),
            )
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


# ---------------------------------------------------------------- preferences


THEMES = {"light", "dark", "system"}
YEAR = 60 * 60 * 24 * 365


def _safe_next(target: str | None) -> str:
    """Only a path on this site: never `//elsewhere` or a full URL."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


@router.get("/preferences")
def preferences(
    theme: str | None = None,
    views3d: str | None = None,
    next: str | None = None,
) -> Response:
    """Display preferences only (theme, 3D views), in plain cookies (`07-design.md` §4.1, §11.6)."""
    response = RedirectResponse(_safe_next(next), status_code=303)
    if theme in THEMES:
        if theme == "system":
            response.delete_cookie(THEME_COOKIE, path="/")
        else:
            response.set_cookie(THEME_COOKIE, theme, max_age=YEAR, samesite="lax", path="/")
    if views3d in {"on", "off"}:
        response.set_cookie(VIEWS_3D_COOKIE, views3d, max_age=YEAR, samesite="lax", path="/")
    return response


# ---------------------------------------------------------------- pages


def _shell(request: Request, user: SignedIn) -> dict[str, Any]:
    """What the sidebar shows on every page: nav counts for approvers, where we are."""
    with transaction(request) as conn:
        counts = queries.approval_counts(conn, user.actor)
    return {
        "nav_counts": counts,
        "path": request.url.path,
        "idle_seconds": _settings(request).session_idle_hours * 3600,
    }


def app_page(
    request: Request, name: str, user: SignedIn, status: int = 200, **context: Any
) -> Response:
    error = request.query_params.get("error")
    return page(
        request,
        name,
        user,
        status,
        **_shell(request, user),
        error_code=error if error in ERROR_WORDS else ("unknown" if error else None),
        **context,
    )


@router.get("/")
def queue_page(
    request: Request,
    user: CurrentUser,
    *,
    status: CaseStatus | None = None,
    exception_type: FindingCode | None = None,
    vendor_id: uuid.UUID | None = None,
    q: str | None = None,
    cursor: str | None = None,
) -> Response:
    with transaction(request) as conn:
        cases = queries.list_cases(
            conn,
            user.actor,
            status=status,
            exception_type=exception_type,
            vendor_id=vendor_id,
            q=q,
            oldest_first=True,
            cursor=cursor,
        )
        counts = queries.queue_counts(conn, user.actor)
        vendors = queries.queue_vendors(conn, user.actor)
    filters = {
        k: v
        for k, v in {
            "status": status.value if status else None,
            "exception_type": exception_type.value if exception_type else None,
            "vendor_id": str(vendor_id) if vendor_id else None,
            "q": q.strip() if q and q.strip() else None,
        }.items()
        if v
    }
    return app_page(
        request,
        "queue.html",
        user,
        cases=cases,
        counts=counts,
        vendors=vendors,
        filters=filters,
        filter_query=urlencode(filters),
        statuses=list(CaseStatus),
        finding_codes=list(FindingCode),
        first_page=cursor is None,
        desk=desk_queue(counts, user.role, filters.get("status")),
    )


@router.get("/approvals")
def approvals_page(
    request: Request,
    user: CurrentUser,
    status: str | None = None,
    cursor: str | None = None,
) -> Response:
    held = status == CaseStatus.HELD_FOR_APPROVER.value
    with transaction(request) as conn:
        cases = queries.list_approvals(conn, user.actor, held=held, cursor=cursor)
        counts = queries.approval_counts(conn, user.actor)
    desk = desk_approvals(counts, user.actor.approval_unlimited, held)
    return app_page(request, "approvals.html", user, cases=cases, held=held, desk=desk)


@router.get("/cases/{case_id}")
def case_page(request: Request, case_id: uuid.UUID, user: CurrentUser) -> Response:
    with transaction(request) as conn:
        case = queries.case_detail(conn, user.actor, case_id)
        run_id = (case["evidence_run"] or case["latest_run"] or {}).get("id")
        trace = queries.run_trace(conn, user.actor, case_id, run_id) if run_id else None
        timeline = queries.run_timeline(conn, user.actor, case_id, run_id) if run_id else {}
        audit = queries.case_audit(conn, user.actor, case_id, limit=100)
        progress = (
            queries.progress(conn, user.actor, case_id)
            if case["status"] in {CaseStatus.NEW, CaseStatus.INVESTIGATING}
            else None
        )
    params = request.query_params
    page_no = int(params["page"]) if params.get("page", "").isdigit() else 1
    run = case["evidence_run"]
    # The table is the default and authoritative view; 3D is an optional view beside it.
    show_3d = params.get("evidence") == "3d"
    scenes = {
        "match_stack": match_stack(
            case["match_strip"], run["checks"] if run else [], case["findings"]
        )
        if case["match_strip"]
        else None,
        "resolution_chain": resolution_chain(case["findings"], case["supporting_documents"]),
        "pipeline": pipeline_track(trace, timeline) if trace else None,
        "pipeline_live": pipeline_live(progress) if progress else None,
        "audit_chain": audit_chain(audit),
        "document_fan": document_fan(
            case["invoice"],
            case["supporting_documents"],
            page=page_no,
            highlight=params.get("highlight"),
            previews=bool(case["invoice"]["redaction_supported"] and run),
        ),
    }
    toggle = "?" + urlencode({**params, "evidence": "table" if show_3d else "3d"}) + "#evidence"
    return app_page(
        request,
        "case.html",
        user,
        case=case,
        trace=trace,
        audit=audit,
        progress=progress,
        supporting_kinds=list(SupportingKind),
        scenes=scenes,
        show_3d=show_3d,
        evidence_toggle=toggle,
    )


@router.get("/cases/{case_id}/_progress")
def case_progress(request: Request, case_id: uuid.UUID, user: CurrentUser) -> Response:
    """Polled every 2 s while investigating; once the run ends, the page reloads itself."""
    with transaction(request) as conn:
        progress = queries.progress(conn, user.actor, case_id)
    if progress["status"] not in {CaseStatus.NEW, CaseStatus.INVESTIGATING}:
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return page(
        request,
        "_progress.html",
        user,
        case_id=case_id,
        progress=progress,
        live=pipeline_live(progress),
        oob=True,
    )


@router.get("/uploads/new")
def upload_page(request: Request, user: CurrentUser) -> Response:
    queries.require_role(user.actor, UserRole.ANALYST)
    return app_page(
        request,
        "upload.html",
        user,
        max_mb=_settings(request).upload_max_bytes // (1024 * 1024),
    )
