FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS builder

ENV VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/nonexistent \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MASK_GC_HOST=0.0.0.0 \
    MASK_GC_PORT=8771

RUN python -m pip install --no-cache-dir "uv==0.11.29"

WORKDIR /app
COPY apps/labeling-mask-gc/pyproject.toml apps/labeling-mask-gc/uv.lock ./apps/labeling-mask-gc/
COPY apps/labeling-mask-gc/src/ ./apps/labeling-mask-gc/src/
COPY apps/assets-mask-content/pyproject.toml ./apps/assets-mask-content/pyproject.toml
COPY apps/assets-mask-content/src/aap_assets_mask_content/ ./apps/assets-mask-content/src/aap_assets_mask_content/

WORKDIR /app/apps/labeling-mask-gc
RUN uv venv /opt/venv \
    && uv sync --locked --no-dev --extra server --no-editable

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/nonexistent \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MASK_GC_HOST=0.0.0.0 \
    MASK_GC_PORT=8771
RUN groupadd --system --gid 10003 labeling-mask-gc \
    && useradd --system --uid 10003 --gid labeling-mask-gc --home-dir /nonexistent labeling-mask-gc
COPY --from=builder --chown=10003:10003 /opt/venv /opt/venv
WORKDIR /runtime
USER 10003:10003
EXPOSE 8771
HEALTHCHECK --interval=30s --timeout=3s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8771/health/ready', timeout=2)"]
CMD ["python", "-m", "aap_labeling_mask_gc_runtime"]
