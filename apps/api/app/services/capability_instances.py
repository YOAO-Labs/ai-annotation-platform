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

import logging

from aap_inference import extract_capabilities
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.inference_backend_control import (
    BackendControlOperationContextFactory,
    BackendControlService,
)
from app.services.inference_limits import capability_limits_from_settings

logger = logging.getLogger(__name__)


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


async def load_capability_instances(
    db: AsyncSession,
    *,
    operation_context_factory: BackendControlOperationContextFactory,
) -> list[dict]:
    """Build the public capability catalog from sanitized facts and owner setup."""
    control = BackendControlService(
        db, operation_context_factory=operation_context_factory
    )
    backends = await control.catalog_rows()
    if not backends:
        return []

    instances: list[dict] = []
    for backend in backends:
        health_meta = backend.health_meta
        caps = (
            health_meta.get("capabilities") if isinstance(health_meta, dict) else None
        )
        if not _shape_models(caps):
            setup_result = await control.setup(backend.registry_id)
            setup = setup_result.setup
            caps = (
                extract_capabilities(setup, limits=capability_limits_from_settings())
                if setup
                else None
            )
        models = _shape_models(caps)
        if not models:
            logger.debug(
                "instances: skip backend %s (no models in snapshot or live probe)",
                backend.name,
            )
            continue
        instances.append(
            {
                "backend_id": str(backend.registry_id),
                "state": backend.state,
                "source": backend.source,
                "name": backend.name,
                "infra": (caps or {}).get("infra") or "unknown",
                "warmup_endpoint": bool((caps or {}).get("warmup_endpoint", False)),
                "models": models,
            }
        )
    return instances
