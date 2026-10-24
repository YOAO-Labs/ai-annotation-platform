FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS build
RUN python -m pip install --no-cache-dir uv==0.11.29
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /build
COPY apps/_shared/owner_data /_shared/owner_data
COPY apps/_shared/identity_admission /_shared/identity_admission
COPY apps/_shared/inference_contract /_shared/inference_contract
COPY apps/_shared/workspace_contract /_shared/workspace_contract
COPY apps/_shared/task_contract /_shared/task_contract
COPY apps/identity /identity
COPY apps/notifications/pyproject.toml /notifications/pyproject.toml
COPY apps/notifications/src /notifications/src
COPY apps/assets-source /assets-source
COPY apps/assets-cleanup /assets-cleanup
COPY apps/_shared/audit_contract /_shared/audit_contract
COPY apps/workspace/pyproject.toml apps/workspace/uv.lock ./
RUN uv sync --frozen --no-dev --extra runtime --no-install-project
COPY apps/workspace/src ./src
COPY apps/workspace/README.md ./README.md
RUN uv sync --frozen --no-dev --extra runtime --no-editable

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
RUN groupadd --gid 10004 workspace && useradd --uid 10004 --gid 10004 --no-create-home workspace
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10004:10004
EXPOSE 8172
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('WORKSPACE_PORT', '8172') + '/ready', timeout=2)"
CMD ["python", "-m", "aap_workspace"]
