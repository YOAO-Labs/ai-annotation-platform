# onnxtools-backend · 二阶段车辆属性预标注 (rtdetr 检测 + va 车辆属性).
#
# Base: nvidia/cuda cudnn9 runtime (cuda12.8 + cudnn9 满足 onnxruntime-gpu 1.22 的 GPU 需求).
# 早期版本压在 pytorch *-devel base 上 (~17GB), 但 onnxtools 链路根本不依赖 torch
# (依赖树里声明 torch 的只有 torchvision/torchaudio, 全是 pytorch base 自带的死重);
# onnxtools[inference] 实际只要 onnxruntime-gpu + opencv. 改用 nvidia/cuda runtime base 后
# 系统 cuDNN/CUDA 走标准 ldconfig 路径, onnxruntime CUDAExecutionProvider 直接可用,
# 既删掉 torch 三件套又免去 LD_LIBRARY_PATH hack, 镜像 ~18GB → ~3GB. 缺 GPU 时仍自动 fallback CPU.
#
# 端口 8004 (compose profile gpu-onnxtools); 与 gsam2(8001)/sam3(8002)/yolo(8003) 解耦.
#
# build context 是 apps/ (与其余 backend 一致); 所有 COPY 路径以 apps/ 为根.
#   docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-onnxtools build onnxtools-backend
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# nvidia/cuda base 无 python; 装 python3.10(ubuntu22.04 默认) + pip + opencv 运行库 + git.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        git curl ca-certificates libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 先升级构建工具: ubuntu22.04 自带 setuptools 59 不支持 PEP 660, 对 src-layout 的 `-e`
# editable 会退回 legacy 模式、把 .pth 指到包根而非 src/ → import 失败. 必须在装 protocol_v2 之前升级.
RUN python3 -m pip install --upgrade pip "setuptools>=68" wheel

# 共享 protocol_v2 包 (apps/_shared/protocol_v2). 与其余 backend 共用, 单一来源.
COPY _shared/task_contract/ /app/task_contract/
COPY _shared/protocol_v2/ /app/protocol_v2/
RUN python3 -m pip install --no-build-isolation -e /app/task_contract -e /app/protocol_v2

# 共享 backend_runtime 包 (apps/_shared/backend_runtime). 与其余 backend 共用的无状态叶子函数
# (图片下载 / /versions 载荷). pillow 经此 editable 安装补上 (onnxtools 不直接声明)。
COPY _shared/backend_runtime/ /app/backend_runtime/
RUN python3 -m pip install --no-build-isolation -e /app/backend_runtime

# backend 自身轻依赖 + onnxtools。固定已审计提交，避免依赖私有 ORT session
# ownership 字段的 residency / cleanup 契约随上游 main 漂移。
RUN python3 -m pip install "fastapi>=0.110" "uvicorn[standard]>=0.27" "pydantic>=2.5" "httpx>=0.27" "numpy>=1.24,<3.0" \
    && python3 -m pip install "onnxtools[inference] @ git+https://github.com/yyq19990828/onnxtools.git@102fcfeb08fc00f865c334a181bfd0bb518e5ed8"

COPY onnxtools-backend/main.py onnxtools-backend/predictor.py \
     onnxtools-backend/schemas.py onnxtools-backend/attribute_schema.py \
     onnxtools-backend/handle_pool.py onnxtools-backend/gpu_lifecycle.py ./
COPY onnxtools-backend/scripts/validate_managed_lifecycle.py \
     /app/scripts/validate_managed_lifecycle.py

# 模型目录: rtdetr-2024080100.onnx + va_260612.onnx 由 volume 挂载注入 (见 docker-compose.ml.yml).
RUN mkdir -p /app/models

EXPOSE 8004

# 系统 cuDNN/CUDA 在标准 ldconfig 路径, onnxruntime 自动找到 → 无需 LD_LIBRARY_PATH hack.
ENTRYPOINT ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8004", "--workers", "1"]
