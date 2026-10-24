# OpsPilot-Finance-Agent
### An evidence-gathering agent for invoice and procurement exceptions, with human approval before any consequential action.

OpsPilot investigates finance paperwork the way an analyst would: it pulls the related purchase order and receipt, checks the invoice's math and details, consults the procurement policy, explains any discrepancy with cited evidence, and pauses for human approval before acting. It is evaluated on a labeled synthetic dataset of injected exception types against a plain LLM-plus-SQL baseline.

> Status: project skeleton. Features are being built in phases; this README grows with them.

## Run locally

Requirements: Docker (or a free alternative such as Colima) and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env        # fill in the values; secrets need 32+ characters
docker compose up --build   # db, migrations, app on http://127.0.0.1:8000, worker
```

Development without containers:

```bash
uv sync
uv run pytest               # tests never call a live LLM
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Everything here runs at zero cost: local services and the Gemini API free tier only.
