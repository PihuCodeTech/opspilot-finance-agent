"""Shared, non-LLM preprocessing: one parse per document that both systems read.

Docling parses each PDF (text layer, layout, tables with cell structure and
coordinates). Bank numbers are replaced by fingerprint tokens in the text, the
structure and the coordinates before anything is stored. The result is written
once per (document, parser version), insert-only; reruns skip what's done.

Scanned images need OCR (RapidOCR), which arrives with the scanned path in P15.
"""

import io
import time
import uuid
from dataclasses import dataclass
from functools import cached_property
from importlib.metadata import version
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from opspilot.db.models import Dataset, Document, Invoice, ParsedDocument
from opspilot.extraction.bank_tokens import Tokeniser

log = structlog.get_logger()

PARSER_VERSION = f"docling-{version('docling')}+fast-tables+tok2"


class UnsupportedDocumentError(RuntimeError):
    pass


@dataclass(frozen=True)
class Parsed:
    text: str
    structure: dict[str, Any]
    coordinates: dict[str, Any]
    bank_tokens: list[dict[str, str]]
    seconds: float


def _coordinates(document: Any) -> dict[str, Any]:
    """Text items and table cells with page and bounding box, for evidence highlighting."""
    items: list[dict[str, Any]] = []
    for item, _level in document.iterate_items():
        text = getattr(item, "text", None)
        for prov in getattr(item, "prov", []) or []:
            if text:
                box = prov.bbox
                items.append(
                    {
                        "text": text,
                        "page": prov.page_no,
                        "bbox": [box.l, box.t, box.r, box.b],
                        "origin": str(box.coord_origin.value),
                    }
                )
    cells: list[dict[str, Any]] = []
    for table_index, table in enumerate(document.tables):
        page = table.prov[0].page_no if table.prov else None
        for cell in table.data.table_cells:
            if cell.bbox is not None:
                box = cell.bbox
                cells.append(
                    {
                        "table": table_index,
                        "row": cell.start_row_offset_idx,
                        "col": cell.start_col_offset_idx,
                        "text": cell.text,
                        "page": page,
                        "bbox": [box.l, box.t, box.r, box.b],
                        "origin": str(box.coord_origin.value),
                    }
                )
    return {"items": items, "table_cells": cells}


class Preprocessor:
    """Holds one Docling converter per process (loading its models takes seconds)."""

    def __init__(self, bank_key: str, artifacts_path: str | None = None) -> None:
        self.bank_key = bank_key
        self.artifacts_path = artifacts_path

    @cached_property
    def _converter(self) -> Any:
        # Imported lazily: Docling pulls in PyTorch and takes seconds to import.
        from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
        from docling.datamodel.pipeline_options import (  # noqa: PLC0415
            PdfPipelineOptions,
            TableFormerMode,
            TableStructureOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: PLC0415

        options = PdfPipelineOptions(
            do_ocr=False,
            do_table_structure=True,
            table_structure_options=TableStructureOptions(mode=TableFormerMode.FAST),
        )
        if self.artifacts_path:
            options.artifacts_path = self.artifacts_path
        return DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )

    def parse(self, path: Path, mime_type: str) -> Parsed:
        if mime_type != "application/pdf":
            raise UnsupportedDocumentError("scanned images need the OCR path (P15)")
        started = time.monotonic()
        from docling_core.types.io import DocumentStream  # noqa: PLC0415

        # A neutral name: the storage file name (a content hash) must not enter the parse.
        stream = DocumentStream(name="document.pdf", stream=io.BytesIO(path.read_bytes()))
        document = self._converter.convert(stream).document
        tokeniser = Tokeniser(self.bank_key)
        text = tokeniser.text(document.export_to_markdown())
        structure = tokeniser.structure(document.export_to_dict())
        coordinates = tokeniser.structure(_coordinates(document))
        return Parsed(text, structure, coordinates, tokeniser.tokens(), time.monotonic() - started)


def preprocess_document(
    engine: Engine, preprocessor: Preprocessor, document_id: uuid.UUID, store: Path
) -> bool:
    """Parse and store one document; returns False when it was already done."""
    with Session(engine) as session:
        done = session.scalar(
            select(ParsedDocument.id).where(
                ParsedDocument.document_id == document_id,
                ParsedDocument.parser_version == PARSER_VERSION,
            )
        )
        if done is not None:
            return False
        document = session.get_one(Document, document_id)
        path, mime = store / document.storage_path, document.mime_type
    parsed = preprocessor.parse(path, mime)
    statement = (
        insert(ParsedDocument)
        .values(
            document_id=document_id,
            parser_version=PARSER_VERSION,
            text=parsed.text,
            structure=parsed.structure,
            coordinates=parsed.coordinates,
            ocr_confidence=None,
            bank_tokens=parsed.bank_tokens,
        )
        .on_conflict_do_nothing(index_elements=["document_id", "parser_version"])
    )
    with Session(engine) as session, session.begin():
        session.execute(statement)
    log.info("preprocess.done", document_id=str(document_id), seconds=round(parsed.seconds, 2))
    return True


def pending_documents(engine: Engine, dataset_name: str, dataset_version: str) -> list[uuid.UUID]:
    with Session(engine) as session:
        done = select(ParsedDocument.document_id).where(
            ParsedDocument.parser_version == PARSER_VERSION
        )
        rows = session.scalars(
            select(Invoice.document_id)
            .join(Dataset, Dataset.id == Invoice.dataset_id)
            .where(Dataset.name == dataset_name, Dataset.version == dataset_version)
            .where(Invoice.document_id.not_in(done))
            .order_by(Invoice.received_at, Invoice.id)
        )
        return list(rows)
