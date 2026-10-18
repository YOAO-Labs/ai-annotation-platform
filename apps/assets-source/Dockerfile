FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

ENV VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASSETS_SOURCE_HOST=0.0.0.0 \
    ASSETS_SOURCE_PORT=8767

RUN python -m pip install --no-cache-dir "uv==0.11.29" \
    && groupadd --system --gid 10003 assets-source \
    && useradd --system --uid 10003 --gid assets-source --home-dir /nonexistent assets-source

WORKDIR /app
COPY apps/assets-source/pyproject.toml apps/assets-source/uv.lock ./apps/assets-source/
COPY apps/assets-source/src/ ./apps/assets-source/src/
COPY apps/_shared/audit_contract/pyproject.toml ./apps/_shared/audit_contract/
COPY apps/_shared/audit_contract/src/ ./apps/_shared/audit_contract/src/
COPY apps/assets-cleanup/pyproject.toml ./apps/assets-cleanup/
COPY apps/assets-cleanup/src/ ./apps/assets-cleanup/src/
COPY apps/assets-media/pyproject.toml ./apps/assets-media/
COPY apps/assets-media/src/ ./apps/assets-media/src/
WORKDIR /app/apps/assets-source
RUN uv venv /opt/venv \
    && uv sync --locked --no-dev --extra server --no-editable \
    && chown -R 10003:10003 /app /opt/venv

USER 10003:10003
EXPOSE 8767
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8767/ready', timeout=2)"]
CMD ["python", "-m", "aap_assets_source_runtime"]
