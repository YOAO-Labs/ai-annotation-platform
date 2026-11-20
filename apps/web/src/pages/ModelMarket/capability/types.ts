// 能力目录的共享类型(从 CapabilityCatalogPanel.tsx 拆出,行为零变化)。

import type { MLBackendItem } from "@/api/adminMlIntegrations";
import type { CapabilityWarning, MLModelCapability } from "@/api/ml-backends";

// 一个展开后的 model 条目 (附带其来源 backend, 供分组/过滤/标题用).
export interface FlatModel {
  model: MLModelCapability;
  backendId: string; // env-only 合成 id 或某条 registered backend id
  backendName: string;
  projectId: string; // env_only="", registered=主 project_id (仅供 capabilities API)
  projectName: string; // 单 project 名 (向后兼容); 多 project 时取首个
  // 来源: env_only = docker-compose 自带 / observe-only;
  //            registered = 已注册到具体项目 (projectName 即注册项目).
  source: "env_only" | "registered";
  // 同 URL 跨多项目注册时, 这里聚合所有注册项目名. env-only 留空。
  registeredProjects: string[];
  // backend 默认 infra / modalities, model 缺省时回落.
  backendInfra?: string;
  backendModalities?: string[];
  healthMeta?: MLBackendItem["health_meta"];
  warmupEndpoint?: boolean;
  stale: boolean;
  // 该 model 命中的受控词表校验诊断 (越界 task/prompt/geometry); 缺/空 = 合法。
  warnings?: CapabilityWarning[];
}

export type CatalogViewMode = "cards" | "list";
export type CatalogGroupBy = "none" | "backend" | "task" | "infra";

// 列表行结构. 一行 = 一个物理权重 (一份 .pt 文件).
// 两条渲染策略:
//   ① variants_shared_across_tasks=true (gsam2): 同 backend 内多 task 共用同 axis_key 的权重,
//      按 (backend, axis_key, axis_value) 聚合, task 列汇总所有用到此权重的 task;
//      行名直接是 variant label (例: "SAM 2.1 Tiny")。
//   ② variants_shared_across_tasks=false (yolo): 每 (model.task, axis0_value) 一行,
//      行名加 task 后缀 (例: "YOLOv8-OBB"); axis1 仍在变体列横展。
//   ③ 0 axes (sam3): 单行 fallback, 行名=display_name。
export interface ListRow {
  parent: FlatModel;
  rowKey: string;
  primaryLabel: string;
  primaryId: string;
  tasks: string[]; // 行所覆盖的 task id 列表 (shared=true 时可能 >1).
  geometries: string[]; // 行的输出几何 (跨 task 取 union, 去重保序).
  secondaryLabel: string; // variants 列 (axis1 或 vram 元信息)
  secondaryTitle?: string;
  warmVariants: Record<string, string>;
  runtimeKey?: string;
}
