FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS build
ENV VIRTUAL_ENV=/opt/venv UV_PROJECT_ENVIRONMENT=/opt/venv
RUN python -m pip install --no-cache-dir "uv==0.11.29"
WORKDIR /build
COPY apps/preannotation-results/pyproject.toml apps/preannotation-results/uv.lock apps/preannotation-results/README.md ./
COPY apps/preannotation-results/src/ ./src/
RUN uv venv /opt/venv && uv sync --locked --no-dev --extra postgres --no-editable

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
ENV VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --system --gid 10004 preannotation-results \
    && useradd --system --uid 10004 --gid preannotation-results --home-dir /nonexistent preannotation-results
COPY --from=build /opt/venv /opt/venv
WORKDIR /runtime
USER 10004:10004
ENTRYPOINT ["python", "-m", "aap_preannotation_results_runtime"]
CMD ["--help"]
