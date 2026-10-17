FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /usr/local/bin/

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 codex
WORKDIR /app
ENV UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev
COPY app ./app
RUN mkdir -p /data/codex /data/workspaces && chown -R codex:codex /data /app

USER codex
ENV DATA_DIR=/data CODEX_HOME=/data/codex WORKSPACE_ROOT=/data/workspaces
EXPOSE 8000
STOPSIGNAL SIGTERM
CMD ["uv", "run", "--locked", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "30"]
