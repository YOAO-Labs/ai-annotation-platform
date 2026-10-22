FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS build
RUN python -m pip install --no-cache-dir uv==0.11.29
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /build
COPY apps/_shared/owner_data /_shared/owner_data
COPY apps/preannotation-results /preannotation-results
COPY apps/assets-media /assets-media
COPY apps/assets-mask-content /assets-mask-content
COPY apps/labeling/pyproject.toml apps/labeling/uv.lock ./
RUN uv sync --frozen --no-dev --extra runtime --no-install-project
COPY apps/labeling/src ./src
COPY apps/labeling/README.md ./README.md
RUN uv sync --frozen --no-dev --extra runtime --no-editable

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime
RUN groupadd --gid 10006 labeling && useradd --uid 10006 --gid 10006 --no-create-home labeling
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10006:10006
EXPOSE 8178
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8178/ready', timeout=2)"
CMD ["python", "-m", "aap_labeling"]

FROM runtime AS video-writer
EXPOSE 8180
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('LABELING_VIDEO_WRITER_PORT','8180')+'/ready', timeout=2)"
CMD ["aap-labeling-video-writer"]

FROM runtime AS class-query
