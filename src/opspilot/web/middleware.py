"""Request IDs and security headers (`06-architecture.md` §16)."""

import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from opspilot.web.uploads import FileTooLargeError

# Everything is served from this origin: htmx, three.js and the fonts are vendored.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

REQUEST_ID_HEADER = "X-Request-ID"

CallNext = Callable[[Request], Awaitable[Response]]


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers.update(SECURITY_HEADERS)
        if not request.url.path.startswith("/static/"):
            # Case data, evidence and sessions: never kept by a browser or proxy cache.
            response.headers.setdefault("Cache-Control", "no-store")
        return response


class BodySizeLimitMiddleware:
    """Refuse request bodies over the cap before anything parses them.

    Starlette's multipart parser has no total limit, so an oversized upload
    would be spooled to disk first. A declared `Content-Length` over the cap
    gets 413 at once; an undeclared (chunked) body is counted as it streams.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _too_large(scope, receive, send)
            return
        seen = 0

        async def counted() -> Message:
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > self.max_bytes:
                # A ServiceError, so the app answers 413 in its usual shape.
                raise FileTooLargeError("The request is over the size limit.")
            return message

        try:
            await self.app(scope, counted, send)
        except FileTooLargeError:  # raised before the app could answer
            await _too_large(scope, receive, send)


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        {
            "error": {
                "code": "file_too_large",
                "message": "The request is over the size limit.",
                "details": {},
                "request_id": None,
            }
        },
        status_code=413,
        headers={"Connection": "close", **SECURITY_HEADERS},
    )
    await response(scope, receive, send)
