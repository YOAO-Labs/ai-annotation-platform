FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

ENV VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASSETS_CATALOG_HOST=0.0.0.0 \
    ASSETS_CATALOG_PORT=8766

RUN python -m pip install --no-cache-dir "uv==0.11.29" \
    && groupadd --system --gid 10002 assets-catalog \
    && useradd --system --uid 10002 --gid assets-catalog --home-dir /nonexistent assets-catalog

WORKDIR /app
COPY apps/assets-catalog/pyproject.toml apps/assets-catalog/uv.lock ./
COPY apps/assets-catalog/src/ ./src/
RUN uv venv /opt/venv \
    && uv sync --locked --no-dev --extra server --no-editable \
    && chown -R 10002:10002 /app /opt/venv

USER 10002:10002
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/ready', timeout=2)"]
CMD ["python", "-m", "aap_assets_catalog"]
