"""Documents of live cases: the original as an attachment, or a rendered page image.

Nothing is served inline from the original bytes: a PDF or image downloads
with `Content-Disposition: attachment` and `nosniff` (set for every response
by the middleware), and pages are re-rendered PNGs (`06-architecture.md` §16).
"""

import hashlib
import uuid
from functools import partial
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter
from fastapi.responses import Response
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from opspilot.cases import queries, service
from opspilot.cases.errors import ConflictError, NotFoundError, ValidationFailedError
from opspilot.cases.queries import require_reader
from opspilot.config import Settings
from opspilot.db.models import ParsedDocument
from opspilot.extraction.preprocess import PARSER_VERSION
from opspilot.web.auth import CurrentUser
from opspilot.web.db import transaction
from opspilot.web.pages import (
    NO_IMAGE_PREVIEW,
    Box,
    PreviewUnavailableError,
    find_boxes,
    find_redactions,
    render_page,
)
from opspilot.web.uploads import JPEG, PDF, PNG

router = APIRouter()
log = structlog.get_logger()
EXTENSIONS = {PDF: "pdf", PNG: "png", JPEG: "jpg"}


def _store(request: Request) -> Path:
    settings: Settings = request.app.state.settings
    return Path(settings.document_store_path)


NO_STORE = {"Cache-Control": "private, no-store", "Pragma": "no-cache"}
SCAN_NOTICE = "unredacted-scan: bank details in scanned images are not redacted until OCR (P15)"


class IntegrityError(ConflictError):
    code = "integrity_failed"


@router.get("/api/v1/documents/{document_id}")
def download(request: Request, document_id: uuid.UUID, user: CurrentUser) -> Response:
    """The original, byte for byte, after checking it against its recorded hash.

    Every download is written to the audit trail before the bytes leave, and the
    response must never be cached (the original keeps the printed bank number).
    """
    require_reader(user.actor)
    with transaction(request) as conn:
        document = queries.require_visible_document(conn, document_id)
    path = _store(request) / document["storage_path"]
    if not path.is_file():
        raise NotFoundError("the document file is missing")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != document["sha256"]:
        log.error("document.integrity_failed", document_id=str(document_id))
        raise IntegrityError("The stored file doesn't match its recorded hash; it isn't served.")
    with transaction(request) as conn:
        service.record_document_download(conn, user.actor.id, document_id, document["sha256"])
    name = f"document-{str(document_id)[:8]}.{EXTENSIONS[document['mime_type']]}"
    headers = {"Content-Disposition": f'attachment; filename="{name}"', **NO_STORE}
    if document["mime_type"] != PDF:
        headers["X-OpsPilot-Notice"] = SCAN_NOTICE
    return Response(data, media_type=document["mime_type"], headers=headers)


def _highlight(highlight: str | None) -> uuid.UUID | None:
    if highlight is None:
        return None
    kind, _, value = highlight.partition(":")
    try:
        if kind == "finding":
            return uuid.UUID(value)
    except ValueError:
        pass
    raise ValidationFailedError("highlight is finding:<id>", field="highlight")


@router.get("/api/v1/documents/{document_id}/pages/{page}.png")
async def page_image(
    request: Request,
    user: CurrentUser,
    document_id: uuid.UUID,
    page: int,
    highlight: str | None = None,
) -> Response:
    require_reader(user.actor)
    finding_id = _highlight(highlight)

    def boxes_for() -> tuple[dict[str, Any], list[Box], list[Box]]:
        with transaction(request) as conn:
            document = queries.require_visible_document(conn, document_id)
            values = (
                queries.highlight_values(conn, document_id, finding_id)
                if finding_id is not None
                else []
            )
            coordinates = conn.execute(
                select(ParsedDocument.coordinates)
                .where(ParsedDocument.document_id == document_id)
                # The current parser's output, as the agent reads it (`agent/graph.py`).
                .order_by(
                    (ParsedDocument.parser_version == PARSER_VERSION).desc(),
                    ParsedDocument.created_at.desc(),
                    ParsedDocument.id,
                )
                .limit(1)
            ).scalar()
        if document["mime_type"] != PDF:
            raise PreviewUnavailableError(NO_IMAGE_PREVIEW)
        if coordinates is None:
            # No parse yet means no list of where the bank details are: fail closed.
            raise PreviewUnavailableError(
                "The preview is available once the document has been parsed."
            )
        return document, find_boxes(coordinates, values, page), find_redactions(coordinates, page)

    document, boxes, redactions = await run_in_threadpool(boxes_for)
    png = await run_in_threadpool(
        partial(render_page, redactions=redactions), _store(request), document, page, boxes
    )
    return Response(png, media_type="image/png", headers=NO_STORE)
