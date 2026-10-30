# CUDA 12.8 + cuDNN 9 runtime, no Torch dependency. Build context: apps/.
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip git curl ca-certificates libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# The Git source is fixed to an audited commit; all registry artifacts are
# hash-locked. GUI OpenCV requirements are intentionally replaced by contrib
# headless in the runtime export (see README).
ARG PIP_FIND_LINKS
ARG PIP_TRUSTED_HOST
COPY onnxtools-backend/requirements-build.txt onnxtools-backend/requirements-runtime.txt \
     onnxtools-backend/requirements-vcs.txt /build/
RUN python3 -m pip install --require-hashes --no-deps -r /build/requirements-build.txt \
    && python3 -m pip install --require-hashes --no-deps --no-build-isolation -r /build/requirements-runtime.txt \
    && python3 -m pip install --no-deps --no-build-isolation -r /build/requirements-vcs.txt

COPY _shared/task_contract/ /build/task_contract/
COPY _shared/protocol_v2/ /build/protocol_v2/
COPY _shared/lifecycle_contract/ /build/lifecycle_contract/
COPY onnxtools-backend/pyproject.toml onnxtools-backend/main.py onnxtools-backend/predictor.py \
     onnxtools-backend/schemas.py onnxtools-backend/attribute_schema.py onnxtools-backend/handle_pool.py \
     onnxtools-backend/gpu_lifecycle.py /build/onnxtools-backend/
RUN python3 -m pip install --no-build-isolation --no-deps \
        /build/task_contract /build/protocol_v2 /build/lifecycle_contract /build/onnxtools-backend
COPY _shared/requirements-plugin-runtime.txt /build/requirements-plugin-runtime.txt
RUN python3 -m pip install --no-deps --require-hashes -r /build/requirements-plugin-runtime.txt \
    && rm -rf /build
COPY onnxtools-backend/scripts/validate_managed_lifecycle.py /app/scripts/
RUN mkdir -p /app/models
EXPOSE 8004
ENTRYPOINT ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8004", "--workers", "1"]
