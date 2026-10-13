# Base image / 编译工具链与官仓 IDEA-Research/Grounded-SAM-2/Dockerfile 对齐:
#   - cuda12.1-cudnn8-devel (而非 runtime): GroundingDINO Deformable Attention 算子需 nvcc 现场编译
#   - gcc-10 / g++-10: 官方显式锁定避开新版编译器与 CUDA 12.1 的不兼容
# torch / torchvision 已由 base image 预装 (2.3.1 + 0.18.1), pyproject.toml 不重复锁.
FROM pytorch/pytorch:2.3.1-cuda12.1-cudnn8-devel

ARG USE_CUDA=1
# 默认覆盖 V100 (7.0) / T4 (7.5) / A100 (8.0) / RTX 30 (8.6) / RTX 40 / Ada (8.9) / H100 (9.0).
# 编译期决定可用 GPU 架构, 主机驱动需 >= 525 (CUDA 12.1 minimum).
ARG TORCH_ARCH="7.0;7.5;8.0;8.6;8.9;9.0"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.cache/huggingface \
    AM_I_DOCKER=True \
    BUILD_WITH_CUDA=${USE_CUDA} \
    TORCH_CUDA_ARCH_LIST=${TORCH_ARCH} \
    CUDA_HOME=/usr/local/cuda-12.1/ \
    PATH=/usr/local/cuda-12.1/bin:${PATH} \
    LD_LIBRARY_PATH=/usr/local/cuda-12.1/lib64:${LD_LIBRARY_PATH}

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        ninja-build \
        gcc-10 \
        g++-10 \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV CC=gcc-10 \
    CXX=g++-10

# 平台 backend 自身依赖 (不含 torch / torchvision; base image 已锁 2.3.1 / 0.18.1).
RUN pip install --upgrade "pip" "setuptools>=62.3.0,<75.9" "wheel" && \
    pip install \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.27" \
        "pydantic>=2.5" \
        "httpx>=0.27" \
        "numpy>=1.24,<2.0" \
        "pillow>=10.0" \
        "opencv-python-headless>=4.9" \
        "shapely>=2.0" \
        "prometheus-client>=0.20" \
        "huggingface_hub>=0.23,<1.0" \
        "transformers>=4.40,<5" \
        "supervision>=0.21" \
        "addict>=2.4" \
        "pycocotools>=2.0" \
        "timm>=0.9" \
        "yapf" \
        "pynvml>=11.5" \
        "psutil>=5.9"

# v0.9.4 phase 3: build context 升级为 apps/ 以便 COPY 兄弟目录 _shared/mask_utils.
# 所有 COPY 路径以 apps/ 为根; 历史命令 `docker build apps/grounded-sam2-backend/` 不再可用,
# 必须走 `docker compose --profile gpu build grounded-sam2-backend` 或 `docker build -f apps/grounded-sam2-backend/Dockerfile apps/`.

# vendor 含上游 IDEA-Research/Grounded-SAM-2 全树:
#   - 仓库根 = SAM 2 (sam2 包) → pip install -e .
#   - grounding_dino/ 子目录 = GroundingDINO (含 CUDA 算子) → pip install --no-build-isolation -e .
COPY grounded-sam2-backend/vendor/ ./vendor/
RUN if [ ! -d "vendor/grounded-sam-2" ] || [ -z "$(ls -A vendor/grounded-sam-2 2>/dev/null)" ]; then \
        echo "ERROR: vendor/grounded-sam-2 is empty. Run scripts/sync_vendor.sh <commit> on host before 'docker build'." >&2; \
        exit 1; \
    fi && \
    pip install --no-build-isolation -e ./vendor/grounded-sam-2 && \
    pip install --no-build-isolation -e ./vendor/grounded-sam-2/grounding_dino

# v0.9.4 phase 3: 共享 mask_utils 包 (apps/_shared/mask_utils) 在容器内安装为 editable;
# 与 v0.10.x sam3-backend 共用, 单一来源避免重写 mask→polygon 简化逻辑.
COPY _shared/mask_utils/ /app/mask_utils/
RUN pip install --no-build-isolation -e /app/mask_utils

# v0.14.12 · 共享协议 v2 schema + 受控词表常量 (apps/_shared/protocol_v2),
# 与 sam3-backend / yolo-backend 共用, 避免协议字段在 backend 之间漂移.
COPY _shared/protocol_v2/ /app/protocol_v2/
# Base image pins pyOpenSSL 24.0 against cryptography<43. Resolve both together
# now that the shared lifecycle protocol requires a newer cryptography release.
RUN pip install --no-build-isolation -e /app/protocol_v2 "pyOpenSSL>=24.2.1"

# 共享 backend_runtime 包 (apps/_shared/backend_runtime). 五 backend 共用的无状态叶子函数
# (图片下载 / 显存释放 / /versions 载荷). setuptools 已在装 protocol_v2 前处理, 复用.
COPY _shared/backend_runtime/ /app/backend_runtime/
RUN pip install --no-build-isolation -e /app/backend_runtime

COPY grounded-sam2-backend/scripts/ ./scripts/
COPY grounded-sam2-backend/main.py grounded-sam2-backend/predictor.py \
     grounded-sam2-backend/schemas.py grounded-sam2-backend/embedding_cache.py \
     grounded-sam2-backend/model_pool.py \
     grounded-sam2-backend/video_predictor.py grounded-sam2-backend/video_pool.py \
     grounded-sam2-backend/pool_domain.py grounded-sam2-backend/gpu_lifecycle.py \
     grounded-sam2-backend/observability.py ./
COPY grounded-sam2-backend/tests/ ./tests/
RUN mkdir -p /app/checkpoints /app/.cache/huggingface

EXPOSE 8001

# 仅阻塞下载主变体 (单档, 秒级~分钟级) 让 uvicorn 尽快起; 额外 PREFETCH 变体由 app
# startup 后台下 (边服务边补, /health.provisioning 可见进度), 避免全量 ~3GB 阻塞启动期间容器对外 error.
ENTRYPOINT ["/bin/bash", "-c", "python /app/scripts/download_checkpoints.py primary && exec uvicorn main:app --host 0.0.0.0 --port 8001 --workers 1"]
