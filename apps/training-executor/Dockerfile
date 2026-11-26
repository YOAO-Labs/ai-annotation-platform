FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY packages/python-sdk /packages/python-sdk
RUN python -m pip install /packages/python-sdk httpx==0.28.1 psutil==7.0.0
COPY apps/training-executor/training_executor/ ./training_executor/
RUN groupadd --gid 10001 yoao \
    && useradd --uid 10001 --gid 10001 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin yoao \
    && chmod -R a+rX /app/training_executor

USER 10001:10001

ENTRYPOINT ["python", "-m", "training_executor.worker"]
