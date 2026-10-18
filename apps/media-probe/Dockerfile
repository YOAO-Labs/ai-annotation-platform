FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

ENV VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir "uv==0.11.29" \
    && groupadd --system --gid 10001 media-probe \
    && useradd --system --uid 10001 --gid media-probe --home-dir /nonexistent media-probe

WORKDIR /app
COPY apps/media-probe/pyproject.toml apps/media-probe/uv.lock ./
COPY apps/media-probe/src/ ./src/
RUN uv venv /opt/venv \
    && uv sync --locked --no-dev --extra server --no-editable \
    && chown -R 10001:10001 /app /opt/venv

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "aap_media_probe.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
