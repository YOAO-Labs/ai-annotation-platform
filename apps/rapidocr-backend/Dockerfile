# rapidocr-backend · RapidOCR ML backend（OCR：det 原子 + rec 原子 + e2e composite）。
#
# Base 与瘦身后的 onnxtools-backend 共享 nvidia/cuda 12.8 runtime。RapidOCR 额外固定
# cuDNN 9.10.2 并将其动态库路径置于 base 内置 9.8 之前：PP-OCRv6 medium 含非对称
# padding 的卷积，9.8 会在 RTX 3090 上触发 CUDNN_FE HEURISTIC_QUERY_FAILED 并使 ORT
# 整条 session 回退 CPU；9.10 修复该卷积引擎支持。应用只在明确设备错误时构造
# CPU replacement；显式 CPU 模式不申请 GPU。
#
# 端口 8005（compose profile gpu-rapidocr）；与 gsam2(8001)/sam3(8002)/yolo(8003)/onnxtools(8004) 解耦。
#
# build context 是 apps/（与其余 backend 一致）；所有 COPY 路径以 apps/ 为根。
#   docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-rapidocr build rapidocr-backend
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# nvidia/cuda base 无 python；装 python3.10(ubuntu22.04 默认) + pip + opencv/pyclipper/shapely 运行/编译库。
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        git curl ca-certificates \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# 先升级构建工具（同 onnxtools：ubuntu 自带 setuptools 59 不支持 PEP 660，src-layout 的 -e 会指错目录）。
RUN python3 -m pip install --upgrade pip "setuptools>=68" wheel

# 共享 protocol_v2 包（apps/_shared/protocol_v2）。与其余 backend 共用，单一来源。
COPY _shared/task_contract/ /app/task_contract/
COPY _shared/protocol_v2/ /app/protocol_v2/
RUN python3 -m pip install --no-build-isolation -e /app/task_contract -e /app/protocol_v2

# 共享 backend_runtime 包 (apps/_shared/backend_runtime). 仅消费 versions_payload
# (RapidOCR.load_img 自带下载, 不用 fetch_image)。与其余 backend 单一来源。
COPY _shared/backend_runtime/ /app/backend_runtime/
RUN python3 -m pip install --no-build-isolation -e /app/backend_runtime

# RapidOCR（pin v3.9.0）+ GPU onnxruntime + backend 轻依赖。RapidOCR 拉 opencv_python(全量)，
# 容器无 GUI → 换 opencv-python-headless（仿 RapidOCR 官方 Dockerfile）。
# 可选 build arg 只用于指向已校验的本地/CI wheelhouse；缺省仍使用标准 pip index。
ARG PIP_FIND_LINKS
ARG PIP_TRUSTED_HOST
RUN if [ -n "$PIP_FIND_LINKS" ]; then \
        python3 -m pip install --no-index --no-deps \
            "rapidocr==3.9.0" "onnxruntime-gpu==1.23.2" "numpy==2.2.6" \
            "opencv-python==5.0.0.93" "opencv-python-headless==5.0.0.93" \
            "nvidia-cudnn-cu12==9.10.2.21"; \
    fi
RUN python3 -m pip install "rapidocr==3.9.0" "onnxruntime-gpu==1.23.2" \
        "cryptography>=43.0.0" "PyJWT>=2.10,<3" \
        "fastapi>=0.110" "uvicorn[standard]>=0.27" "pydantic>=2.5" "httpx>=0.27" "numpy>=1.24,<3.0" "Pillow>=10" \
    && python3 -m pip uninstall -y opencv-python opencv-contrib-python 2>/dev/null; \
       python3 -m pip install "opencv-python-headless>=4.5.1"

# wheelhouse 路径会先装 GUI/headless 两个共享 cv2 文件的 distribution；卸载 GUI 版后
# 强制恢复 headless 文件。标准索引路径不预装 headless，因此无需重复安装。
RUN if [ -n "$PIP_FIND_LINKS" ]; then \
        python3 -m pip install --no-index --no-deps --force-reinstall \
            "opencv-python-headless==5.0.0.93"; \
    fi

# Base 已提供 CUDA 12.8 依赖，只覆盖 cuDNN；禁止 wheel 额外拉取重复的 CUDA/cuBLAS。
RUN python3 -m pip install --no-deps "nvidia-cudnn-cu12==9.10.2.21"

ENV LD_LIBRARY_PATH=/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib:/usr/local/cuda/lib64

COPY rapidocr-backend/main.py rapidocr-backend/predictor.py \
     rapidocr-backend/schemas.py rapidocr-backend/catalog.py \
     rapidocr-backend/engine_pool.py rapidocr-backend/gpu_lifecycle.py ./
COPY rapidocr-backend/scripts/validate_managed_lifecycle.py /app/scripts/

# 模型目录：13 个 onnx 由 bind mount 注入（download_models.py 下载，见 docker-compose.ml.yml）。
RUN mkdir -p /app/models

ENV RAPIDOCR_MODEL_DIR=/app/models \
    RAPIDOCR_DEVICE=gpu

EXPOSE 8005

# CUDA 使用 base 路径；cuDNN 使用上方固定的 9.10.2 wheel 路径。
ENTRYPOINT ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005", "--workers", "1"]
