# CUDA 12.8 runtime with a pinned cuDNN 9.10.2 override for PP-OCRv6.
# Build context: apps/.
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip git curl ca-certificates \
        libglib2.0-0 libsm6 libxext6 libxrender1 gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Hash-locked upstream artifacts; the runtime export intentionally substitutes
# headless OpenCV for the GUI distribution required by RapidOCR metadata.
ARG PIP_FIND_LINKS
ARG PIP_TRUSTED_HOST
COPY rapidocr-backend/requirements-build.txt rapidocr-backend/requirements-runtime.txt /build/
RUN python3 -m pip install --require-hashes --no-deps -r /build/requirements-build.txt \
    && python3 -m pip install --require-hashes --no-deps --no-build-isolation -r /build/requirements-runtime.txt

COPY _shared/task_contract/ /build/task_contract/
COPY _shared/protocol_v2/ /build/protocol_v2/
COPY _shared/lifecycle_contract/ /build/lifecycle_contract/
COPY rapidocr-backend/pyproject.toml rapidocr-backend/main.py rapidocr-backend/predictor.py \
     rapidocr-backend/schemas.py rapidocr-backend/catalog.py rapidocr-backend/engine_pool.py \
     rapidocr-backend/gpu_lifecycle.py rapidocr-backend/download_models.py \
     rapidocr-backend/plugin_assets.py /build/rapidocr-backend/
RUN python3 -m pip install --no-build-isolation --no-deps \
        /build/task_contract /build/protocol_v2 /build/lifecycle_contract /build/rapidocr-backend
COPY _shared/requirements-plugin-runtime.txt /build/requirements-plugin-runtime.txt
RUN python3 -m pip install --no-deps --require-hashes -r /build/requirements-plugin-runtime.txt \
    && rm -rf /build
COPY rapidocr-backend/scripts/validate_managed_lifecycle.py /app/scripts/
RUN mkdir -p /app/models

# Base supplies CUDA libraries; the pinned wheel supplies cuDNN 9.10.2.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib:/usr/local/cuda/lib64 \
    RAPIDOCR_MODEL_DIR=/app/models \
    RAPIDOCR_DEVICE=gpu
EXPOSE 8005
ENTRYPOINT ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005", "--workers", "1"]
