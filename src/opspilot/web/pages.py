"""Server-rendered page images with evidence boxes (`05-stack.md`: no client PDF library).

A finding's checks cite invoice values (`evidence_refs`). Those values are
found in the stored parse's coordinates (Docling text items and table cells)
and boxed on the page image. Numbers compare by value, so "1,185.00" on the
page matches "1185.00" in the evidence. Renders are cached on disk by
document, page and the exact boxes drawn.

**Bank numbers never appear in a preview** (owner decision on P11). It fails closed:
- every region the parse tokenised is blacked out, and so is every account
  number pdfium's own text layer finds on the page (two independent detections);
- a page with no text layer (a scan inside a PDF) and every image upload are
  refused (409 `preview_unavailable`) until OCR-based redaction exists (P15).
"""

import hashlib
import io
import json
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from opspilot.cases.errors import ConflictError, NotFoundError
from opspilot.extraction.bank_tokens import find_account_numbers
from opspilot.web.uploads import PDF

MAX_SIDE_PX = 2000  # a page never renders larger than this, whatever its declared size
SCALE = 2.0  # 144 dpi for a normal page
MAX_BOXES = 24
MIN_TEXT_MATCH = 3  # shorter non-numeric values ("1", "A") would box half the page
BOX_COLOUR = (217, 119, 6)  # amber: visible on white paper in both UI themes
REDACTION_COLOUR = (17, 17, 17)
BANK_TOKEN = "BANK_ACCT["  # noqa: S105  # a marker in parsed text, not a secret
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


class PreviewUnavailableError(ConflictError):
    code = "preview_unavailable"


NO_IMAGE_PREVIEW = (
    "Previews of scanned images are disabled: bank details can't be redacted until OCR"
    " arrives (P15). Authorised reviewers can download the original."
)
NO_TEXT_LAYER = (
    "This page has no text layer (a scan inside a PDF), so bank details can't be"
    " redacted yet (P15). Authorised reviewers can download the original."
)


@dataclass(frozen=True)
class Box:
    page: int
    bbox: tuple[float, float, float, float]  # l, t, r, b in PDF points
    origin: str


def _number(value: str) -> Decimal | None:
    cleaned = value.replace(",", "").replace("$", "").replace("€", "").replace("£", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _matches(text: str, value: str) -> bool:
    wanted = _number(value)
    if wanted is not None:
        return any(_number(token) == wanted for token in _NUMBER.findall(text))
    needle = value.strip().lower()
    return len(needle) >= MIN_TEXT_MATCH and needle in text.lower()


def find_boxes(coordinates: dict[str, Any], values: list[str], page: int) -> list[Box]:
    """Boxes on `page` whose text holds one of the cited values (table cells first)."""
    boxes: list[Box] = []
    entries = [*coordinates.get("table_cells", []), *coordinates.get("items", [])]
    for value in dict.fromkeys(values):
        for entry in entries:
            if entry.get("page") != page or not isinstance(entry.get("text"), str):
                continue
            if _matches(entry["text"], value):
                box = Box(page, tuple(entry["bbox"]), str(entry.get("origin", "")))
                if box not in boxes:
                    boxes.append(box)
                if len(boxes) >= MAX_BOXES:
                    return boxes
    return boxes


def find_redactions(coordinates: dict[str, Any], page: int) -> list[Box]:
    """Every region on `page` whose parsed text held a bank account (stored as a token).

    The original file must keep the number (it is the invoice), but a rendered page
    never shows it: bank numbers are never sent in full (`rules.md` §2.7).
    """
    entries = [*coordinates.get("table_cells", []), *coordinates.get("items", [])]
    return [
        Box(page, tuple(e["bbox"]), str(e.get("origin", "")))
        for e in entries
        if e.get("page") == page and isinstance(e.get("text"), str) and BANK_TOKEN in e["text"]
    ]


def _pixels(box: Box, height_pt: float, scale: float) -> tuple[float, float, float, float]:
    left, top, right, bottom = box.bbox
    if box.origin.upper().startswith("BOTTOM"):
        top, bottom = height_pt - top, height_pt - bottom
    y0, y1 = sorted((top, bottom))
    pad = 2
    return (left * scale - pad, y0 * scale - pad, right * scale + pad, y1 * scale + pad)


def account_boxes(page: Any, page_no: int) -> list[Box]:
    """Account numbers in the page's own text layer; refuses a page that has none."""
    textpage = page.get_textpage()
    try:
        count = textpage.count_chars()
        text = textpage.get_text_range(0, count) if count > 0 else ""
        if not text.strip():
            raise PreviewUnavailableError(NO_TEXT_LAYER)
        boxes = []
        for span in find_account_numbers(text):
            for index in range(span.start, min(span.end, count)):
                left, bottom, right, top = textpage.get_charbox(index)
                boxes.append(Box(page_no, (left, top, right, bottom), "BOTTOMLEFT"))
        return boxes
    finally:
        textpage.close()


def _render_pdf(data: bytes, page_no: int) -> tuple[Image.Image, float, float, list[Box]]:
    document = pdfium.PdfDocument(data)
    try:
        if not 1 <= page_no <= len(document):
            raise NotFoundError("page not found")
        page = document[page_no - 1]
        found = account_boxes(page, page_no)
        width, height = page.get_size()
        scale = min(SCALE, MAX_SIDE_PX / max(width, height, 1))
        image = page.render(scale=scale).to_pil().convert("RGB")
        return image, height, scale, found
    finally:
        document.close()


def render_page(
    store: Path,
    document: dict[str, Any],
    page_no: int,
    boxes: list[Box],
    *,
    redactions: list[Box] | None = None,
    cache_dir: Path | None = None,
) -> bytes:
    """The page as PNG: bank numbers blacked out, then the evidence boxes drawn.

    PDFs only: an image has no coordinates to redact by, so it is refused.
    """
    if document["mime_type"] != PDF:
        raise PreviewUnavailableError(NO_IMAGE_PREVIEW)
    hidden = redactions or []
    key = hashlib.sha256(
        json.dumps(
            [[[b.bbox, b.origin] for b in boxes], [[b.bbox, b.origin] for b in hidden]],
            default=str,
        ).encode()
    ).hexdigest()[:16]
    cache = (cache_dir or store / "cache" / "pages") / f"{document['sha256']}-{page_no}-{key}.png"
    if cache.exists():
        return cache.read_bytes()
    path = store / document["storage_path"]
    if not path.is_file():
        raise NotFoundError("the document file is missing")
    data = path.read_bytes()
    image, height_pt, scale, found = _render_pdf(data, page_no)
    draw = ImageDraw.Draw(image)
    for box in [*hidden, *found]:
        draw.rectangle(_pixels(box, height_pt, scale), fill=REDACTION_COLOUR)
    for box in boxes:
        draw.rectangle(_pixels(box, height_pt, scale), outline=BOX_COLOUR, width=4)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    png = out.getvalue()
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_name(f".{uuid.uuid4().hex}.part")
    temporary.write_bytes(png)
    temporary.replace(cache)
    return png
