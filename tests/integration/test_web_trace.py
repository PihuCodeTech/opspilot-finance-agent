"""The run trace over HTTP, for a run the agent graph really made (stand-in model)."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine

from opspilot.cases import service
from opspilot.db.seed import seed
from opspilot.policy.loader import load_policy
from tests.integration.agent_support import BANK_FP, TOKEN, Invoice, job, make_agent, make_scenario
from tests.integration.web_support import make_web

pytestmark = pytest.mark.integration


def test_trace_lists_steps_calls_and_tools_with_bank_data_masked(
    owner_engine: Engine, migrated_url: str, conn: Connection, tmp_path: Path
) -> None:
    seed(owner_engine, load_policy(), demo_password="local-demo-password")
    s = make_scenario(owner_engine)
    agent = make_agent(s.engine, migrated_url, s.model)
    try:
        invoice_id = s.invoice(Invoice(f"INV-{uuid.uuid4().hex[:8].upper()}", s.po_number, "0.45"))
        with s.engine.begin() as c:
            case_id = service.create_case(c, s.analyst, invoice_id)
        agent.investigate_job(s.engine, job(case_id))
    finally:
        agent.checkpointer.conn.close()

    web = make_web(conn, tmp_path)
    client = web.client(s.analyst)
    detail = client.get(f"/api/v1/cases/{case_id}")
    assert detail.status_code == 200 and detail.json()["status"] == "needs_review"
    run_id = detail.json()["evidence_run"]["id"]
    response = client.get(f"/api/v1/cases/{case_id}/runs/{run_id}/trace")
    assert response.status_code == 200
    trace = response.json()
    assert trace["run"]["status"] == "succeeded"
    assert [c["purpose"] for c in trace["llm_calls"]][:1] == ["extract"]
    assert {"investigate", "explain"} <= {c["purpose"] for c in trace["llm_calls"]}
    assert {t["name"] for t in trace["tool_calls"]} == {"search_policy", "get_record"}
    assert all("result" in t for t in trace["tool_calls"])
    assert all(step["done"] for step in trace["steps"])
    assert trace["errors"] == []
    for body in (detail.text, response.text):
        assert BANK_FP not in body and TOKEN not in body
    other_case = web.world.case([])
    wrong = client.get(f"/api/v1/cases/{other_case}/runs/{run_id}/trace")
    assert wrong.status_code == 404  # a run is only reachable through its own case
