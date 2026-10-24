"""FastAPI application: health endpoints and the base page shell.

No application features yet (Step 10); routes for cases, approvals and uploads
arrive in P11.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from opspilot.config import get_settings
from opspilot.db.session import check_readiness, get_engine
from opspilot.logs import configure_logging
from opspilot.web.middleware import RequestContextMiddleware
from opspilot.web.theme import explicit_theme, views_3d_enabled

_WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_WEB_DIR / "templates")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="OpsPilot", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestContextMiddleware)
    app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")

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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"theme": explicit_theme(request), "views_3d": views_3d_enabled(request)},
        )

    return app
