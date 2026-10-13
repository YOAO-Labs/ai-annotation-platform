#!/usr/bin/env python3
"""幂等下载 SAM 3 checkpoint + config.

启动容器时由 Dockerfile ENTRYPOINT 调用; 已下载则跳过, 缺失则从 HuggingFace 拉.
图像模型 sam3.pt 下载失败 sys.exit(1) 让容器启动失败 (避免带半残模型上线);
视频 multiplex 权重默认随镜像一并拉取, 失败仅 warn 而非 exit, 避免把"未来可能用"
的依赖变成线上故障 (可用 SAM3_DOWNLOAD_VIDEO=0 关闭)。

文件清单:
  - facebook/sam3/sam3.pt                 (~3 GB; 图像模型: PCS + inst 交互, 见下; 必拉)
  - facebook/sam3.1/sam3.1_multiplex.pt   (~3.2 GB; 视频追踪权重, 默认拉, 见 SAM3_DOWNLOAD_VIDEO)
  - facebook/sam3.1/config.json           (视频模型配置, 与 multiplex 同步)

为什么图像侧用 sam3.pt 而非 sam3.1_multiplex.pt:
  sam3.1_multiplex.pt 本质是视频模型 (config.architectures=["Sam3VideoModel"]); vendored
  sam3 代码的 image + inst (SAM1-task: point / 单框单 mask) 路径是按 sam3.pt 写的
  (官方 sam3_for_sam1_task_example.ipynb 即 build_sam3_image_model(enable_inst_interactivity=
  True) 默认下载 sam3.pt)。把 3.1 视频权重塞进 3.0 形状的 inst 模型会因 key 命名/结构不匹配
  导致 inst 权重加载失败 → 噪声 mask。故图像交互单模型走 sam3.pt; multiplex 保留供后续视频追踪。

⚠️ facebook/sam3 与 facebook/sam3.1 均为 gated repo, 必须配置 HF_TOKEN 并分别在 HuggingFace
   接受 license: https://huggingface.co/facebook/sam3 · https://huggingface.co/facebook/sam3.1

为什么不直接靠 vendor 内置 download_ckpt_from_hf?
  - 启动时 fail-fast: HF_TOKEN 缺失 / license 没接受 / 网络不通 → 立刻报错退出, 不让
    uvicorn 起来后第一次 /predict 才挂掉
  - 走 docker volume gsam3_checkpoints 持久化, 避免每次重启重新下数 GB
  - 与 grounded-sam2-backend 的启动脚本风格统一

Env:
    HF_TOKEN                    = HuggingFace access token (required for image, video 时另需 license)
    CHECKPOINT_DIR              = /app/checkpoints (default)
    SAM3_IMAGE_HF_REPO_ID       = facebook/sam3 (default; 图像模型仓库)
    SAM3_IMAGE_CHECKPOINT_FILE  = sam3.pt (default; 图像 PCS + inst 权重)
    SAM3_DOWNLOAD_VIDEO         = 1 (default; 0 → 关闭, 跳过 multiplex + config 下载)
    SAM3_HF_REPO_ID             = facebook/sam3.1 (default; 视频 multiplex 仓库, gated, 需独立 license)
    SAM3_CHECKPOINT_FILE        = sam3.1_multiplex.pt (default; 视频追踪权重)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CHECKPOINT_DIR = Path(os.getenv("CHECKPOINT_DIR", "/app/checkpoints"))
# 图像模型 (PCS + inst 交互): sam3.pt。
IMAGE_HF_REPO_ID = os.getenv("SAM3_IMAGE_HF_REPO_ID", "facebook/sam3")
IMAGE_CHECKPOINT_FILE = os.getenv("SAM3_IMAGE_CHECKPOINT_FILE", "sam3.pt")
# 视频 multiplex (预留, 后续视频追踪用)。
HF_REPO_ID = os.getenv("SAM3_HF_REPO_ID", "facebook/sam3.1")
CHECKPOINT_FILE = os.getenv("SAM3_CHECKPOINT_FILE", "sam3.1_multiplex.pt")
CONFIG_FILE = "config.json"


def _download(target: Path, repo_id: str, filename: str, token: str) -> None:
    if target.exists() and target.stat().st_size > 0:
        print(
            f"[skip] {target.name} already exists ({target.stat().st_size // 1024} KB)"
        )
        return
    print(f"[download] {repo_id}/{filename} → {target}")
    from huggingface_hub import hf_hub_download

    cached = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        local_dir=str(target.parent),
        token=token,
    )
    cached_path = Path(cached)
    if cached_path != target:
        cached_path.replace(target)
    print(f"[done] {target} ({target.stat().st_size // 1024} KB)")


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    download_video = _truthy(os.environ.get("SAM3_DOWNLOAD_VIDEO", "1"))

    hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not hf_token:
        print(
            f"ERROR: HF_TOKEN is required to download {IMAGE_HF_REPO_ID} (gated repo).\n"
            "       Set HF_TOKEN in your .env and ensure docker-compose injects it,\n"
            f"       and accept the license at https://huggingface.co/{IMAGE_HF_REPO_ID}",
            file=sys.stderr,
        )
        return 1

    # 图像模型 (当前 /predict 实际加载, 必拉): sam3.pt。失败硬退出。
    try:
        _download(
            CHECKPOINT_DIR / IMAGE_CHECKPOINT_FILE,
            IMAGE_HF_REPO_ID,
            IMAGE_CHECKPOINT_FILE,
            hf_token,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"ERROR: failed to fetch {IMAGE_HF_REPO_ID}/{IMAGE_CHECKPOINT_FILE}: {exc}",
            file=sys.stderr,
        )
        return 1

    # 视频 multiplex + config: 默认随镜像一并拉取 (SAM3_DOWNLOAD_VIDEO=0 可关闭)。
    # 失败只 warn 不 exit —— 视频路径调用时会再校验, 避免 license 没勾就
    # 让整容器起不来 (issue claude[bot] P1)。
    if download_video:
        for target, repo_id, filename in [
            (CHECKPOINT_DIR / CHECKPOINT_FILE, HF_REPO_ID, CHECKPOINT_FILE),
            (CHECKPOINT_DIR / CONFIG_FILE, HF_REPO_ID, CONFIG_FILE),
        ]:
            try:
                _download(target, repo_id, filename, hf_token)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"WARN: failed to fetch {repo_id}/{filename}: {exc}; "
                    "video tracker path will fail at request time until this is resolved.",
                    file=sys.stderr,
                )
    else:
        print(
            "[skip] SAM3_DOWNLOAD_VIDEO=0 → 跳过视频 multiplex 权重; "
            "如需视频追踪请去掉该变量 (或设为 1) 并接受 facebook/sam3.1 license"
        )

    print(f"[ok] sam3 image checkpoint ready in {CHECKPOINT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
