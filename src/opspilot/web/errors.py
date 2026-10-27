"""The one place errors become HTTP responses (`06-api.md` §3).

JSON clients get `{"error": {code, message, details, request_id}}`. Browsers
get a redirect to the sign-in page (401) or a plain error page. Nothing
internal leaks: unexpected errors are logged with the request ID and answered
with a generic message. Validation details name fields, never echo values.
"""

import datetime as dt
import re
import uuid
from decimal import Decimal
from enum import Enum
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from opspilot.cases.errors import ServiceError
from opspilot.web.ratelimit import RateLimitedError

log = structlog.get_logger()


def to_json(value: Any) -> Any:  # noqa: PLR0911  # one branch per type
    """Plain JSON: money and other decimals as strings (never floats), UUIDs, ISO times."""
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_json(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, bytes | memoryview):
        return bytes(value).hex()
    return value


def json_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(to_json(content), status_code=status_code)


def wants_html(request: Request) -> bool:
    """`/api/…` is JSON. `/auth/…` follows the Accept header. Everything else is the UI."""
    path = request.url.path
    if path.startswith("/api/"):
        return False
    if path.startswith("/auth/"):
        return "text/html" in request.headers.get("accept", "")
    return True


def error_body(
    request: Request, code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": to_json(details or {}),
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def _error(
    request: Request,
    status: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    if wants_html(request):
        if status == 401:  # noqa: PLR2004
            return RedirectResponse("/login", status_code=303)
        back = _form_origin(request, status)
        if back is not None:
            return RedirectResponse(f"{back}?error={code}", status_code=303)
        templates: Jinja2Templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status": status, "code": code, "message": message, "user": None},
            status_code=status,
            headers=headers,
        )
    return JSONResponse(
        error_body(request, code, message, details), status_code=status, headers=headers
    )


_CASE_FORM = re.compile(r"^/cases/([0-9a-f-]{36})/[a-z-]+(/[0-9a-f-]{36}/[a-z]+)?$")


def _form_origin(request: Request, status: int) -> str | None:
    """A failed form post goes back to its page with the error code, to be shown inline.

    Only the code travels (`?error=stale_version`); the page words it from a fixed list,
    so nothing the request carried is ever reflected.
    """
    if request.method != "POST" or status == 404:  # noqa: PLR2004
        return None
    path = request.url.path
    if path == "/uploads":
        return "/uploads/new"
    match = _CASE_FORM.match(path)
    return f"/cases/{match.group(1)}" if match else None


def _field_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    return [
        {"field": ".".join(str(p) for p in e.get("loc", ())[1:]), "problem": str(e.get("msg"))}
        for e in exc.errors()
    ]


def install(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    def service_error(request: Request, exc: ServiceError) -> Response:
        headers = (
            {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimitedError) else None
        )
        return _error(
            request, exc.status, exc.code, exc.message, details=exc.details, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    def validation_error(request: Request, exc: RequestValidationError) -> Response:
        if any(e.get("loc", ("",))[0] == "path" for e in exc.errors()):
            return _error(request, 404, "not_found", "Not found.")
        return _error(
            request,
            422,
            "validation_failed",
            "Some fields are invalid.",
            details={"fields": _field_errors(exc)},
        )

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:  # noqa: PLR2004
            return _error(request, 404, "not_found", "Not found.")
        if exc.status_code == 405:  # noqa: PLR2004
            return _error(request, 405, "bad_request", "Method not allowed.")
        return _error(request, exc.status_code, "bad_request", "Bad request.")

    @app.exception_handler(Exception)
    def unexpected(request: Request, exc: Exception) -> Response:
        log.error("request.failed", error=type(exc).__name__, path=request.url.path)
        return _error(request, 500, "internal_error", "Something went wrong.")
