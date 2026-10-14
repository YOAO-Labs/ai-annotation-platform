# The offline profile used torch 2.6.0+cu124 and torchvision 0.21.0+cu124.
# No model download, HF login or credentials are part of this build.
ARG VGGT_OMEGA_BASE_IMAGE=pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:77f17f843507062875ce8be2a6f76aa6aa3df7f9ef1e31d9d7432f4b0f563dee
FROM ${VGGT_OMEGA_BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/vendor

WORKDIR /app
# apps/ is the build context, matching other first-party model services.
COPY vggt-omega-backend/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir "setuptools==75.8.0" "wheel==0.45.1" \
    && pip install --no-cache-dir --require-hashes -r requirements.lock
COPY _shared/protocol_v2/ /app/protocol_v2/
COPY _shared/backend_runtime/ /app/backend_runtime/
RUN pip install --no-deps --no-build-isolation /app/protocol_v2 /app/backend_runtime
COPY vggt-omega-backend/config.py vggt-omega-backend/schemas.py \
     vggt-omega-backend/predictor.py vggt-omega-backend/service.py \
     vggt-omega-backend/gpu_lifecycle.py vggt-omega-backend/main.py ./
COPY vggt-omega-backend/vendor/ /app/vendor/
RUN python -c "from predictor import validate_vendor; validate_vendor()" \
    && python -c "import torch, torchvision; assert torch.__version__ == '2.6.0+cu124'; assert torchvision.__version__ == '0.21.0+cu124'"

EXPOSE 8006
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8006", "--workers", "1"]
