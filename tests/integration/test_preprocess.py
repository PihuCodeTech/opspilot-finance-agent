"""Preprocessing stores one tokenised parse per document, idempotently."""

import hashlib
import shutil
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from opspilot.extraction.preprocess import PARSER_VERSION, pending_documents, preprocess_document
from tests.docling_support import FIXTURES, preprocessor, require_docling, truth

pytestmark = [pytest.mark.integration, pytest.mark.docling]


def test_preprocess_stores_once_and_tracks_pending(owner_engine: Engine, tmp_path: Path) -> None:
    require_docling()
    source = FIXTURES / "demo-classic.pdf"
    data = source.read_bytes()
    sha = hashlib.sha256(data + tmp_path.name.encode()).hexdigest()  # unique per test run
    store = tmp_path / "store"
    (store / "x").mkdir(parents=True)
    shutil.copyfile(source, store / "x" / f"{sha}.pdf")

    with owner_engine.begin() as conn:
        dataset = conn.execute(
            text(
                "INSERT INTO datasets (name, version, split) VALUES ('pre', :v, 'dev') RETURNING id"
            ),
            {"v": sha[:12]},
        ).scalar_one()
        document = conn.execute(
            text(
                "INSERT INTO documents (sha256, kind, mime_type, size_bytes, storage_path, source)"
                " VALUES (:h, 'invoice', 'application/pdf', :n, :p, 'generated') RETURNING id"
            ),
            {"h": sha, "n": len(data), "p": f"x/{sha}.pdf"},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO invoices (dataset_id, document_id, received_at)"
                " VALUES (:d, :doc, now())"
            ),
            {"d": dataset, "doc": document},
        )

    assert pending_documents(owner_engine, "pre", sha[:12]) == [document]
    assert preprocess_document(owner_engine, preprocessor(), document, store) is True
    assert preprocess_document(owner_engine, preprocessor(), document, store) is False
    assert pending_documents(owner_engine, "pre", sha[:12]) == []

    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT parser_version, text, bank_tokens FROM parsed_documents"
                " WHERE document_id = :d"
            ),
            {"d": document},
        ).one()
    assert row.parser_version == PARSER_VERSION
    assert truth("demo-classic")["number"] in row.text
    assert len(row.bank_tokens) == 1
    assert truth("demo-classic")["bank_account"].replace(" ", "") not in row.text.replace(" ", "")
