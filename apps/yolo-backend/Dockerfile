# Build context: apps/. Keep source and binary runtime identities immutable.
FROM mirror.gcr.io/pytorch/pytorch:2.14.1-cuda13.2-cudnn9-runtime@sha256:c4ab67f95221a342dff0e8ca4543a7b8885f79f7a0029c0e2e39685d5eaf1722

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    YOLO_CONFIG_DIR=/app/.config/Ultralytics YOLO_AUTOINSTALL=false \
    OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 MKL_NUM_THREADS=4 \
    POLARS_MAX_THREADS=4 NUMEXPR_MAX_THREADS=4

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git libgl1 libglib2.0-0 fonts-dejavu-core python3.12-venv \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv --system-site-packages /opt/yolo-venv
ENV VIRTUAL_ENV=/opt/yolo-venv PATH=/opt/yolo-venv/bin:$PATH
WORKDIR /app

COPY yolo-backend/requirements.lock ./
RUN python -m pip install --require-hashes --no-deps -r requirements.lock
RUN python -m pip install --no-deps --no-build-isolation \
    "git+https://github.com/ultralytics/CLIP.git@7ffa84b3bfa40c42ecc1c77147a855e69cb2dd40"

COPY _shared/task_contract/ /app/task_contract/
COPY _shared/protocol_v2/ /app/protocol_v2/
COPY _shared/backend_runtime/ /app/backend_runtime/
COPY _shared/training_contract/ /app/training_contract/
RUN python -m pip install --no-deps --no-build-isolation -e /app/task_contract -e /app/protocol_v2 -e /app/backend_runtime -e /app/training_contract
RUN python -c "import torch,torchvision,ultralytics,libreyolo,pynvml; assert torch.__version__ == '2.14.1+cu132'; assert torchvision.__version__ == '0.29.1+cu132'; assert ultralytics.__version__ == '8.4.183'; assert libreyolo.__version__ == '1.6.0'"

COPY yolo-backend/main.py yolo-backend/predictor.py yolo-backend/schemas.py \
    yolo-backend/model_pool.py yolo-backend/model_registry.py \
    yolo-backend/class_names.py yolo-backend/model_metadata.py yolo-backend/observability.py \
    yolo-backend/gpu_lifecycle.py yolo-backend/managed_models.py yolo-backend/libreyolo_adapter.py \
    yolo-backend/sample_evaluations.py yolo-backend/managed_segmentation.py yolo-backend/segmentation_evaluations.py ./
COPY yolo-backend/scripts/ ./scripts/
COPY yolo-backend/tests/ ./tests/
RUN chmod -R a+rX /app && mkdir -p /app/checkpoints /app/.config/Ultralytics \
    && ln -sfn /app/checkpoints/weights /app/weights

EXPOSE 8003
ENTRYPOINT ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "1"]
