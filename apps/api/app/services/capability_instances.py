"""v0.14.11 · 平台已知 backend 实例的能力清单 (与项目级注册解耦).

「协议能力目录」需要回答两个问题:
1. 「平台支持哪些 AI 标注能力?」 → 协议层 (capability_registry.py + /protocol 端点)。
2. 「现在跑着哪些 model 可用?」 → 实例层 (本模块 + /instances 端点)。

v0.19.0 ADR-0044 · 数据源统一为**全局注册表 ml_backend_registry**: env 配置的 backend
启动钩子已自动 upsert 成 source=env 注册行, 不再有 env-only 临时探测分支。每行优先读
health_meta.capabilities 快照, 缺失时 fallback live 探测 /setup。

输出字段裁剪: 只暴露 backend_id / state / source / display_name / infra / models[], 不暴露 url /
gpu_info / cache / pool 等运维敏感信息, 让普通用户安全消费。
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from aap_inference import extract_capabilities
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.inference_limits import capability_limits_from_settings

logger = logging.getLogger(__name__)


def _observe_urls() -> list[str]:
    """env 配置的观测 URL (去重保序). 留空时回退 ml_backend_default_url。"""
    urls = list(settings.ml_backend_observe_urls or [])
    if not urls and settings.ml_backend_default_url:
        urls = [settings.ml_backend_default_url]
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        key = u.rstrip("/")
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


async def _probe_setup(client: httpx.AsyncClient, base: str) -> dict | None:
    """轻量探测 /setup, 失败返回 None (不抛, 上游按缺数据处理)。"""
    try:
        resp = await client.get(f"{base}/setup")
        if resp.status_code == 200:
            return resp.json()
    except (httpx.TimeoutException, httpx.RequestError):
        return None
    except Exception:  # noqa: BLE001 — 响应非 JSON 等
        return None
    return None


def _shape_models(caps: dict | None) -> list[dict]:
    """从 extract_capabilities 返回值取 models[], 字段裁剪到能力相关。

    v0.14.12 起补透传 supported_variants + variant_combinations + model_family,
    支撑前端模型市场列表按主 variant 轴展开「具体模型」行 (yolo 的 series×size
    受 MODEL_MATRIX 约束, 必须用 variant_combinations 严格列举合法组合)。
    """
    if not caps:
        return []
    out: list[dict] = []
    for m in caps.get("models") or []:
        # 守卫:某个 backend 自报 "models": [null] / [str] 等非 dict 元素时,m.get(...)
        # 会抛 AttributeError 直接逃出本路由,/instances 仍然整体 500。在源头跳过即可。
        if not isinstance(m, dict):
            continue
        out.append(
            {
                "id": m.get("id", ""),
                "display_name": m.get("display_name") or m.get("id", ""),
                "task": m.get("task", "unknown"),
                "model_family": m.get("model_family"),
                "infra": m.get("infra"),
                "is_interactive": bool(m.get("is_interactive")),
                "supported_prompts": list(m.get("supported_prompts") or []),
                # v0.19.2 WS0 · 透传一等输入契约 + 资源画像, 让走 /instances 的消费方
                # (模型市场 instances / 全局编排选择器) 与项目级 /capabilities 拿到同一字段集。
                "supported_inputs": list(m.get("supported_inputs") or []),
                "default_input_type": m.get("default_input_type"),
                "resource_profile": dict(m.get("resource_profile") or {}),
                "supported_geometric_outputs": list(
                    m.get("supported_geometric_outputs") or []
                ),
                "supported_trackers": list(m.get("supported_trackers") or []),
                # 协议③ · 属性输出类型 + schema 自描述, 供前端「从 backend 导入属性」.
                "output_attribute_types": list(m.get("output_attribute_types") or []),
                "output_attribute_schema": list(m.get("output_attribute_schema") or []),
                # v0.20.3 · 透传 backend 自报的类别清单 (yolo COCO 等), 供前端「从 backend
                # 预填配置」一键导入类别。extract_capabilities 早已保留, 此前在本层被裁掉。
                "classes": list(m.get("classes") or []),
                "modality": m.get("modality"),
                "supported_variants": list(m.get("supported_variants") or []),
                "variant_combinations": list(m.get("variant_combinations") or []),
                "variants_shared_across_tasks": bool(
                    m.get("variants_shared_across_tasks", False)
                ),
                # v0.14.13 · backend 自报的默认 variant 组合, 供前端 VariantSelector 取初值.
                "default_variants": dict(m.get("default_variants") or {}),
                # 协议 v2.2 · 原子 vs 内部编排维度（缺省 atom）。编排下游 stage 据此过滤。
                "composition": m.get("composition") or "atom",
            }
        )
    return out


async def load_capability_instances(db: AsyncSession) -> list[dict]:
    """读全局注册表 ml_backend_registry → 平台已知 backend 实例清单。

    每行优先读 health_meta.capabilities 快照, 缺失时并发 live 探测 /setup。
    source 取注册行的 source ('manual' | 'env')。
    """
    # v0.19.0 ADR-0044 · 过滤掉 disconnected 行(env reconcile 自动置 disconnected
    # 的注册项 / 长期不可达的 manual 行),与 workers/ml_health.py 的探测口径一致;
    # 避免「能力目录」露出已下线 backend 的旧 health_meta 快照 (v0.19.4 的 GPU/批量
    # 徽标会跟着展示已下线 backend 的设备能力, 误导用户)。
    result = await db.execute(
        select(MLBackendRegistry).where(MLBackendRegistry.state != "disconnected")
    )
    backends = list(result.scalars().all())
    if not backends:
        return []

    # 第一遍: 从 health_meta 快照拿 models; 记录需要 live 探测的行。
    snapshots: list[dict | None] = []
    needs_probe: list[int] = []
    for idx, b in enumerate(backends):
        caps: dict | None = None
        if b.health_meta and isinstance(b.health_meta, dict):
            caps = b.health_meta.get("capabilities")
        if _shape_models(caps):
            snapshots.append(caps)
        else:
            snapshots.append(None)
            needs_probe.append(idx)

    # 第二遍: 并发 live 探测 (只对快照缺 models 的行)。
    if needs_probe:
        timeout = httpx.Timeout(float(settings.ml_health_timeout))
        async with httpx.AsyncClient(timeout=timeout) as client:
            setups = await asyncio.gather(
                *[
                    _probe_setup(client, backends[i].url.rstrip("/"))
                    for i in needs_probe
                ]
            )
        for i, setup in zip(needs_probe, setups):
            snapshots[i] = (
                extract_capabilities(setup, limits=capability_limits_from_settings())
                if setup
                else None
            )

    instances: list[dict] = []
    for b, caps in zip(backends, snapshots):
        models = _shape_models(caps)
        if not models:
            # 探测失败 / 协议 v1 backend 合成单 model 也失败 → 静默 skip。
            logger.debug(
                "instances: skip backend %s (no models in snapshot or live probe)",
                b.name,
            )
            continue
        instances.append(
            {
                "backend_id": str(b.id),
                "state": b.state,
                "source": b.source,
                "name": b.name,
                "infra": (caps or {}).get("infra") or "unknown",
                # v0.14.14: backend 自报是否支持 POST /warmup (协议 §4.4).
                "warmup_endpoint": bool((caps or {}).get("warmup_endpoint", False)),
                "models": models,
            }
        )
    return instances
