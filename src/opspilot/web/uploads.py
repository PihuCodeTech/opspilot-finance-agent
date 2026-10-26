"""Upload validation and content-addressed storage (`06-architecture.md` §16).

- **The type comes from the bytes** (magic numbers), never from the file name or the
  declared content type. PDF, PNG and JPEG only.
- **Size and pages are capped** (`UPLOAD_MAX_BYTES`, at most 20 pages), and the file
  must actually open: pdfium counts PDF pages, Pillow verifies images. Docling never
  runs here; the full parse happens in the worker.
- **Storage is by hash:** files go under `uploads/` in the document store, outside the
  web root, named by their SHA-256. They are never executed and are served only as
  attachments or as rendered page images.
"""

import hashlib
import io
import os
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError
from sqlalchemy import Connection, insert, select
from starlette.datastructures import UploadFile

from opspilot.cases.errors import ConflictError, ServiceError, ValidationFailedError
from opspilot.db.models import Document
from opspilot.domain.enums import DocumentKind, DocumentSource

MAX_PAGES = 20
MAX_IMAGE_PIXELS = 40_000_000  # about 6300 × 6300: far beyond any scanned page
CHUNK = 64 * 1024

PDF, PNG, JPEG = "application/pdf", "image/png", "image/jpeg"


class FileTooLargeError(ServiceError):
    status = 413
    code = "file_too_large"


class UnsupportedMediaTypeError(ServiceError):
    status = 415
    code = "unsupported_media_type"


def unreadable(why: str) -> ValidationFailedError:
    return ValidationFailedError(
        f"The file can't be read: {why}.", field="file", reason="unreadable"
    )


async def read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    """The file's bytes, refusing as soon as it passes the limit."""
    parts, size = [], 0
    while chunk := await upload.read(CHUNK):
        size += len(chunk)
        if size > max_bytes:
            raise FileTooLargeError(f"The file is over the {max_bytes // (1024 * 1024)} MB limit.")
        parts.append(chunk)
    if size == 0:
        raise unreadable("it is empty")
    return b"".join(parts)


def sniff(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return PDF
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    raise UnsupportedMediaTypeError("Only PDF, PNG and JPEG files are accepted.")


def _pdf_pages(data: bytes) -> int:
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        raise unreadable("the PDF is damaged or password-protected") from None
    try:
        return len(document)
    finally:
        document.close()


def _check_image(data: bytes) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise unreadable("the image is too large")
                image.verify()
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
            raise unreadable("the image is damaged") from None
        except Image.DecompressionBombWarning:
            raise unreadable("the image is too large") from None


@dataclass(frozen=True)
class Checked:
    data: bytes
    sha256: str
    mime_type: str
    pages: int


def check(data: bytes) -> Checked:
    """Type by content, then open it and count pages (CPU work: call off the event loop)."""
    mime = sniff(data)
    if mime == PDF:
        pages = _pdf_pages(data)
        if pages == 0:
            raise unreadable("the PDF has no pages")
        if pages > MAX_PAGES:
            raise ValidationFailedError(
                f"The file has {pages} pages; the limit is {MAX_PAGES}.",
                field="file",
                reason="too_many_pages",
            )
    else:
        _check_image(data)
        pages = 1
    return Checked(data, hashlib.sha256(data).hexdigest(), mime, pages)


def relative_path(sha256: str) -> str:
    return f"uploads/{sha256[:2]}/{sha256}"


def write_file(store: Path, checked: Checked) -> str:
    """Write once, atomically; the same content always lands at the same path."""
    relative = relative_path(checked.sha256)
    target = store / relative
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid.uuid4().hex}.part")
        temporary.write_bytes(checked.data)
        os.replace(temporary, target)
    return relative


def save_document(
    conn: Connection,
    checked: Checked,
    storage_path: str,
    kind: DocumentKind,
    uploaded_by: uuid.UUID,
) -> uuid.UUID:
    """A `documents` row. The same supporting file may back several cases; invoices may not."""
    existing = conn.execute(
        select(Document.id, Document.kind).where(Document.sha256 == checked.sha256)
    ).first()
    if existing is not None:
        if kind is DocumentKind.SUPPORTING and existing.kind == DocumentKind.SUPPORTING:
            return uuid.UUID(str(existing.id))
        raise ConflictError("This file has already been uploaded.", code="already_uploaded")
    document_id = uuid.uuid4()
    conn.execute(
        insert(Document).values(
            id=document_id,
            sha256=checked.sha256,
            kind=kind,
            mime_type=checked.mime_type,
            size_bytes=len(checked.data),
            page_count=checked.pages,
            storage_path=storage_path,
            source=DocumentSource.UPLOAD,
            uploaded_by=uploaded_by,
        )
    )
    return document_id
