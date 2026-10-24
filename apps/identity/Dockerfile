FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS builder
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN python -m pip install --no-cache-dir uv==0.11.29
WORKDIR /build
COPY apps/_shared/mail_transport /_shared/mail_transport
COPY apps/_shared/identity_admission /_shared/identity_admission
COPY apps/notifications/pyproject.toml /notifications/pyproject.toml
COPY apps/notifications/src /notifications/src
COPY apps/identity/pyproject.toml apps/identity/uv.lock ./
COPY apps/identity/src ./src
RUN uv sync --locked --no-dev --extra runtime --no-editable

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
ENV VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    IDENTITY_HOST=0.0.0.0 IDENTITY_PORT=8170
RUN groupadd --system --gid 10003 identity \
    && useradd --system --uid 10003 --gid identity --home-dir /nonexistent identity
COPY --from=builder --chown=10003:10003 /opt/venv /opt/venv
WORKDIR /runtime
USER 10003:10003
EXPOSE 8170
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8170/ready', timeout=2)"]
CMD ["python", "-m", "aap_identity_runtime"]
