"""FastAPI application: health, sign-in, the JSON API and the HTML pages.

The app holds no business rules: routes call the case service and its query
functions inside one transaction each (`web/db.py`). Tests can pass their own
transaction factory (a savepoint they roll back) and a rate limiter.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from opspilot.config import Settings, get_settings
from opspilot.db.session import check_readiness, get_engine
from opspilot.logs import configure_logging
from opspilot.web import errors, present
from opspilot.web.db import TransactionFactory, engine_transactions
from opspilot.web.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from opspilot.web.ratelimit import RateLimiter
from opspilot.web.routes import cases, documents, ui

_WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_WEB_DIR / "templates")
templates.env.filters.update(present.filters())
templates.env.globals.update(present.globals_())
# Multipart framing and the form's text fields, on top of the largest file allowed.
FORM_OVERHEAD_BYTES = 64 * 1024


def create_app(
    transactions: TransactionFactory | None = None,
    *,
    settings: Settings | None = None,
    limiter: RateLimiter | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="OpsPilot", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.templates = templates
    app.state.limiter = limiter or RateLimiter()
    app.state.transactions = transactions or engine_transactions(get_engine())
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=settings.upload_max_bytes + FORM_OVERHEAD_BYTES
    )
    app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")
    errors.install(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        readiness = check_readiness(get_engine())
        if readiness.ready:
            return JSONResponse({"status": "ready", "database": "ok", "migrations": "at_head"})
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "not_ready",
                    "message": "The service is not ready.",
                    "details": {
                        "database": "ok" if readiness.database_ok else "unavailable",
                        "migrations": "at_head" if readiness.migrations_at_head else "pending",
                        "reason": readiness.detail,
                    },
                    "request_id": request.state.request_id,
                }
            },
        )

    app.include_router(ui.router)
    app.include_router(cases.router)
    app.include_router(documents.router)
    return app
