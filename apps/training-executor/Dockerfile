FROM mirror.gcr.io/pytorch/pytorch:2.14.1-cuda13.2-cudnn9-runtime@sha256:c4ab67f95221a342dff0e8ca4543a7b8885f79f7a0029c0e2e39685d5eaf1722

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    YOLO_CONFIG_DIR=/state/ultralytics MPLCONFIGDIR=/state/matplotlib \
    OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 MKL_NUM_THREADS=4 \
    POLARS_MAX_THREADS=4 NUMEXPR_MAX_THREADS=4

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 fonts-dejavu-core python3.12-venv && rm -rf /var/lib/apt/lists/*

RUN python -m venv --system-site-packages /opt/training-venv
ENV VIRTUAL_ENV=/opt/training-venv PATH=/opt/training-venv/bin:$PATH

WORKDIR /app
COPY apps/training-executor/requirements.lock ./
RUN python -m pip install --require-hashes --no-deps -r requirements.lock && \
    python -c "import torch,torchvision,ultralytics,libreyolo; assert torch.__version__ == '2.14.1+cu132'; assert torchvision.__version__ == '0.29.1+cu132'; assert ultralytics.__version__ == '8.4.183'; assert libreyolo.__version__ == '1.6.0'"
COPY apps/_shared/task_contract /shared/task_contract
RUN python -m pip install --no-deps /shared/task_contract
COPY apps/_shared/training_contract /shared/training_contract
RUN python -m pip install --no-deps /shared/training_contract
COPY apps/training-executor/training_executor/ ./training_executor/
RUN chmod -R a+rX /app/training_executor

ENTRYPOINT ["python", "-m", "training_executor.worker"]
