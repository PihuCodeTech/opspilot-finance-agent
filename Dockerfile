# One image for app and worker. Python 3.12 slim + uv; runs as a non-root user.
FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
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

RUN useradd --create-home --uid 10001 opspilot \
    && mkdir -p /app/data/documents \
    && chown -R opspilot:opspilot /app/data
USER opspilot

EXPOSE 8000
CMD ["uvicorn", "opspilot.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
