FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime

ENV VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -m pip install --no-cache-dir "uv==0.11.29" \
    && groupadd --system --gid 10005 assets-media \
    && useradd --system --uid 10005 --gid assets-media --home-dir /nonexistent assets-media

WORKDIR /app
COPY apps/assets-media/pyproject.toml apps/assets-media/uv.lock ./apps/assets-media/
COPY apps/assets-media/src/ ./apps/assets-media/src/
COPY apps/media-probe/pyproject.toml ./apps/media-probe/
COPY apps/media-probe/src/ ./apps/media-probe/src/
WORKDIR /app/apps/assets-media
RUN uv venv /opt/venv \
    && uv sync --locked --no-dev --extra runtime --extra server --no-editable \
    && chown -R 10005:10005 /app /opt/venv

FROM runtime AS control
USER 10005:10005
ENV ASSETS_MEDIA_HOST=0.0.0.0 ASSETS_MEDIA_PORT=8768
EXPOSE 8768
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8768/health/ready', timeout=2)"]
CMD ["python", "-m", "aap_assets_media_runtime"]

FROM runtime AS worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
USER 10005:10005
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "aap_assets_media_runtime.worker", "--healthcheck"]
CMD ["python", "-m", "aap_assets_media_runtime.worker"]
