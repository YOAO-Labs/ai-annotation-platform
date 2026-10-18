FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

ENV VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASSET_CLEANUP_HEALTH_HOST=127.0.0.1 \
    ASSET_CLEANUP_HEALTH_PORT=8765

RUN python -m pip install --no-cache-dir "uv==0.11.29" \
    && groupadd --system --gid 10001 assets-cleanup \
    && useradd --system --uid 10001 --gid assets-cleanup --home-dir /nonexistent assets-cleanup

WORKDIR /app
COPY apps/assets-cleanup/pyproject.toml apps/assets-cleanup/uv.lock ./
COPY apps/assets-cleanup/src/ ./src/
RUN uv venv /opt/venv \
    && uv sync --locked --no-dev --extra worker --no-editable \
    && chown -R 10001:10001 /app /opt/venv

USER 10001:10001
HEALTHCHECK --interval=30s --timeout=3s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health/ready', timeout=2)"]
CMD ["python", "-m", "aap_assets_cleanup"]
