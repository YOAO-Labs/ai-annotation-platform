# Base image / 编译工具链对齐 vendor/sam3/README.md (commit 4cbac14):
#   - torch >= 2.7 (vendor README §70 写下限); 选 2.7.1 是该系列最末稳定版
#   - CUDA >= 12.6 (同上下限); 选 12.8 与 vendor 的 cu128 wheel index 对齐
#   - cudnn9-devel (devel 而非 runtime — vendor 某些 CUDA 扩展需 nvcc 现场编译;
#     `cudnn9` 是 cudnn 大版本号必须带的后缀)
# torch / torchvision 已由 base image 预装 (2.7.1 + 对应 torchvision),
# pyproject.toml 不重复锁; 与 grounded-sam2-backend 同款策略.
#
# mirror 注意: 若你的 docker mirror 无该 tag, 备选:
#   - pytorch/pytorch:2.7.1-cuda12.6-cudnn9-devel  (CUDA 12.6 下限)
#   - pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel  (更新更好, 不一定存在)
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel

ARG USE_CUDA=1
# 覆盖 A100 (8.0) / RTX 30 (8.6) / RTX 40 / Ada (8.9) / H100 / H200 (9.0).
# 不含 7.x (V100/T4) — 路线图明确 SAM 3 不部 4060/老卡; sam3 显存吃紧.
ARG TORCH_ARCH="8.0;8.6;8.9;9.0"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.cache/huggingface \
    AM_I_DOCKER=True \
    BUILD_WITH_CUDA=${USE_CUDA} \
    TORCH_CUDA_ARCH_LIST=${TORCH_ARCH} \
    CUDA_HOME=/opt/conda \
    PATH=/opt/conda/bin:${PATH}

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        ninja-build \
        gcc \
        g++ \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 平台 backend 自身依赖 (不含 torch / torchvision; base image 已锁 2.7.1).
# setuptools 锁 <81: 81 起移除 pkg_resources, 而 vendor/sam3/model_builder.py 仍 `import pkg_resources`.
# base image 自带 setuptools 75.8 已满足, 这里不升级即可, 显式上限防止 transient 依赖把它顶上 81+.
RUN pip install --upgrade "pip" "setuptools>=68,<81" "wheel" && \
    pip install \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.27" \
        "pydantic>=2.5" \
        "httpx>=0.27" \
        "numpy>=1.26,<2.0" \
        "pillow>=10.0" \
        "opencv-python-headless>=4.9" \
        "shapely>=2.0" \
        "prometheus-client>=0.20" \
        "huggingface_hub>=0.23" \
        "transformers>=4.45" \
        "supervision>=0.21" \
        "pynvml>=11.5" \
        "psutil>=5.9" \
        "einops>=0.7" \
        "pycocotools>=2.0" \
        "open_clip_torch>=2.24"

# 三个手工补丁是 vendor 真实运行时 import 但 [train] extra 没覆盖的:
#   einops          — sam3/sam/rope.py 用; vendor 只在 [notebooks] extra 里列, 但 notebooks 还
#                     夹带 jupyter/ipywidgets/decord 等 200MB+ 运行时无关包, 不值得整 extra
#   pycocotools     — vendor 在 [dev] 和 [notebooks] extras 里, dev 还夹带 pytest/black/yt-dlp 等
#   open_clip_torch — sam3 text encoder backbone; vendor pyproject 完全漏列, 必须手补
# hydra-core / omegaconf / scipy / scikit-image / scikit-learn / torchmetrics 走下面的
# `pip install -e ./vendor/sam3[train]` 一并装入, 不在这里重复.

# build context 是 apps/ (与 grounded-sam2-backend 一致); 所有 COPY 路径以 apps/ 为根.
# 启动方式: `docker compose --profile gpu-sam3 build sam3-backend`
# 直接 build: `docker build -f apps/sam3-backend/Dockerfile apps/`

# vendor 含上游 facebookresearch/sam3 全树 (commit 见 vendor/sam3/.commit);
# 通过 scripts/sync_vendor.sh 同步并固定 commit.
COPY sam3-backend/vendor/ ./vendor/
# 带 [train] extra: 拉 vendor 自己声明的 hydra-core / omegaconf / scipy / scikit-image /
# scikit-learn / torchmetrics / fvcore / fairscale / submitit / tensorboard / zstandard.
# 部分 train 专用包 (submitit/fairscale/tensorboard) 推理路径用不到, 镜像多吃 ~50MB,
# 远比手工 grep 漏装翻车便宜. notebooks extra 拉 jupyter/ipywidgets 等 200MB+ 不要.
RUN if [ ! -d "vendor/sam3" ] || [ -z "$(ls -A vendor/sam3 2>/dev/null)" ]; then \
        echo "ERROR: vendor/sam3 is empty. Run sam3-backend/scripts/sync_vendor.sh <commit> on host before 'docker build'." >&2; \
        exit 1; \
    fi && \
    pip install --no-build-isolation -e "./vendor/sam3[train]"

# 共享 mask_utils 包 (apps/_shared/mask_utils) editable 安装, 与 grounded-sam2-backend 共用.
COPY _shared/mask_utils/ /app/mask_utils/
RUN pip install --no-build-isolation -e /app/mask_utils

# v0.14.12 · 共享协议 v2 schema + 受控词表常量 (apps/_shared/protocol_v2),
# 与 grounded-sam2-backend / yolo-backend 共用, 避免协议字段在 backend 之间漂移.
COPY _shared/protocol_v2/ /app/protocol_v2/
# Base image may pin an older pyOpenSSL against cryptography. Resolve both
# together now that the lifecycle verifier requires current cryptography.
RUN pip install --no-build-isolation -e /app/protocol_v2 "pyOpenSSL>=24.2.1"

# 共享 backend_runtime 包 (apps/_shared/backend_runtime). 五 backend 共用的无状态叶子函数
# (图片下载 / 显存释放 / /versions 载荷). setuptools 已在装 protocol_v2 前处理, 复用.
COPY _shared/backend_runtime/ /app/backend_runtime/
RUN pip install --no-build-isolation -e /app/backend_runtime

COPY sam3-backend/scripts/ ./scripts/
COPY sam3-backend/main.py sam3-backend/predictor.py sam3-backend/video_predictor.py \
     sam3-backend/pvs_video_predictor.py sam3-backend/schemas.py \
     sam3-backend/embedding_cache.py sam3-backend/observability.py \
     sam3-backend/pool_domain.py \
     sam3-backend/gpu_lifecycle.py ./
COPY sam3-backend/tests/ ./tests/
RUN mkdir -p /app/checkpoints /app/.cache/huggingface

EXPOSE 8002

# 启动时下载 sam3.1 权重 + config (gated, 必须 HF_TOKEN), 然后启 uvicorn.
# 已下载会跳过, 失败 fail-fast 避免半残上线.
ENTRYPOINT ["/bin/bash", "-c", "python /app/scripts/download_checkpoints.py && exec uvicorn main:app --host 0.0.0.0 --port 8002 --workers 1"]
