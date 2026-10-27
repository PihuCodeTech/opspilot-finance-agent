"""A scanned image never reaches a model (owner decision on P11; OCR redaction is P15).

The graph runs on committed data with the real preprocessor and a stand-in
model that records every request it is sent. An image has no text to read
without OCR, so the run must fail with a clear reason before any LLM call.
"""

import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from opspilot.agent.deps import AgentDeps
from opspilot.agent.graph import Agent
from opspilot.cases import service
from opspilot.config import LlmMode
from opspilot.db.seed import seed
from opspilot.extraction.preprocess import Preprocessor
from opspilot.llm.client import LlmClient
from opspilot.policy.loader import load_policy
from tests.integration.agent_support import fts_search, job, make_scenario
from tests.integration.web_support import png_bytes

pytestmark = pytest.mark.integration


def test_an_image_upload_fails_before_any_model_call(
    owner_engine: Engine, migrated_url: str, tmp_path: Path
) -> None:
    seed(owner_engine, load_policy(), demo_password="local-demo-password")
    s = make_scenario(owner_engine)
    data = png_bytes() + uuid.uuid4().bytes  # trailing bytes keep the hash unique
    digest = hashlib.sha256(data).hexdigest()
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / digest).write_bytes(data)
    with s.engine.begin() as conn:
        document = conn.execute(
            text(
                "INSERT INTO documents (sha256, kind, mime_type, size_bytes, page_count,"
                " storage_path, source) VALUES (:h, 'upload', 'image/png', :n, 1, :p, 'upload')"
                " RETURNING id"
            ),
            {"h": digest, "n": len(data), "p": f"uploads/{digest}"},
        ).scalar_one()
        invoice = conn.execute(
            text(
                "INSERT INTO invoices (dataset_id, document_id, received_at)"
                " VALUES (:d, :doc, now()) RETURNING id"
            ),
            {"d": s.dataset, "doc": document},
        ).scalar_one()
        case_id = service.create_case(conn, s.analyst, invoice)
    llm = LlmClient(s.model, LlmMode.LIVE, None, s.engine)
    deps = AgentDeps(
        s.engine, llm, fts_search(s.engine), Preprocessor("k" * 48), tmp_path, max_llm_calls=5
    )
    agent = Agent.connect(deps, migrated_url)
    try:
        agent.investigate_job(s.engine, job(case_id))
    finally:
        agent.checkpointer.conn.close()

    assert s.model.seen == []  # not one request reached the model
    with s.engine.connect() as conn:
        status = conn.execute(
            text("SELECT status::text FROM cases WHERE id = :c"), {"c": case_id}
        ).scalar_one()
        run = conn.execute(
            text("SELECT id, failure_reason FROM investigation_runs WHERE case_id = :c"),
            {"c": case_id},
        ).one()
        calls = conn.execute(
            text("SELECT count(*) FROM llm_calls WHERE run_id = :r"), {"r": run.id}
        ).scalar_one()
        parsed = conn.execute(
            text("SELECT count(*) FROM parsed_documents WHERE document_id = :d"), {"d": document}
        ).scalar_one()
    assert status == "investigation_failed"
    assert "scanned_image_unsupported" in run.failure_reason
    assert calls == 0 and parsed == 0
