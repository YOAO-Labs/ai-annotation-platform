FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS build
RUN python -m pip install --no-cache-dir uv==0.11.29
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /build
COPY apps/_shared/owner_data /_shared/owner_data
COPY apps/_shared/inference_contract /_shared/inference_contract
COPY apps/_shared/workspace_contract /_shared/workspace_contract
COPY apps/_shared/task_contract /_shared/task_contract
COPY apps/_shared/protocol_v2 /_shared/protocol_v2
COPY apps/inference/pyproject.toml apps/inference/uv.lock ./
RUN uv sync --frozen --no-dev --extra runtime --no-install-project
COPY apps/inference/src ./src
COPY apps/inference/README.md ./README.md
RUN uv sync --frozen --no-dev --extra runtime --no-editable

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
RUN groupadd --gid 10008 inference && useradd --uid 10008 --gid 10008 --no-create-home inference
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 INFERENCE_HOST=0.0.0.0 INFERENCE_PORT=8768
USER 10008:10008
EXPOSE 8768
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8768/health', timeout=4)"
CMD ["python", "-m", "aap_inference"]
