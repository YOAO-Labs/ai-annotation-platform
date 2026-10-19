FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS builder

ENV VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MASK_CONTENT_HOST=0.0.0.0 \
    MASK_CONTENT_PORT=8770

RUN python -m pip install --no-cache-dir "uv==0.11.29"

WORKDIR /app/apps/assets-mask-content
COPY apps/assets-mask-content/pyproject.toml apps/assets-mask-content/uv.lock ./
COPY apps/assets-mask-content/src/ ./src/
RUN uv venv /opt/venv \
    && uv sync --locked --no-dev --extra server --no-editable

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/nonexistent \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MASK_CONTENT_HOST=0.0.0.0 \
    MASK_CONTENT_PORT=8770
RUN groupadd --system --gid 10005 assets-mask-content \
    && useradd --system --uid 10005 --gid assets-mask-content --home-dir /nonexistent assets-mask-content
COPY --from=builder --chown=10005:10005 /opt/venv /opt/venv
WORKDIR /runtime
USER 10005:10005
EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/health/ready', timeout=2)"]
CMD ["python", "-m", "aap_assets_mask_content_runtime"]
