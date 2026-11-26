FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS build
RUN python -m pip install --no-cache-dir uv==0.11.29
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /src/apps/workspace
COPY apps/_shared/owner_data /src/apps/_shared/owner_data
COPY apps/_shared/identity_admission /src/apps/_shared/identity_admission
COPY packages/python-sdk /src/packages/python-sdk
COPY apps/_shared/workspace_contract /src/apps/_shared/workspace_contract
COPY apps/identity /src/apps/identity
COPY apps/notifications/pyproject.toml /src/apps/notifications/pyproject.toml
COPY apps/notifications/src /src/apps/notifications/src
COPY apps/assets-source /src/apps/assets-source
COPY apps/assets-cleanup /src/apps/assets-cleanup
COPY apps/_shared/audit_contract /src/apps/_shared/audit_contract
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
CMD ["python", "-m", "yoao_workspace"]
