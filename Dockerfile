# One image for app and worker. Python 3.12 slim + uv; runs as a non-root user.
FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Pango/HarfBuzz for WeasyPrint (synthetic invoice PDFs, P3).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN uv sync --frozen --no-dev

# Docling's layout and table models, downloaded once at build time (free, Hugging Face),
# so preprocessing never downloads at runtime.
ENV DOCLING_ARTIFACTS_PATH=/opt/docling-models
RUN HF_HOME=/tmp/hf docling-tools models download layout tableformer -o /opt/docling-models \
    && rm -rf /tmp/hf

# The policy-search embedding model (bge-small, 384 dims), also fetched once at build time.
# At runtime Hugging Face is offline: nothing is downloaded and nothing leaves the machine.
ENV HF_HOME=/opt/hf-cache
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5', device='cpu')" \
    && chmod -R a+rX /opt/hf-cache
ENV HF_HUB_OFFLINE=1

RUN useradd --create-home --uid 10001 opspilot \
    && mkdir -p /app/data/documents \
    && chown -R opspilot:opspilot /app/data
USER opspilot

EXPOSE 8000
CMD ["uvicorn", "opspilot.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
