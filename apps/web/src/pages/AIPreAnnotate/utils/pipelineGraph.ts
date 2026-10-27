import { normalizeTask } from "@/api/generated/capabilityVocab.gen";
/**
 * v0.18.16 · 受限树形流水线 → react-flow 图的纯函数层.
 *
 * stagesGraph ({sid, parentSid}, parentSid="root"=源) 仍是唯一真值; 这里把它派生成 react-flow
 * 的 nodes/edges (分层布局) + 提供编辑期校验 (改父合法性 / 加子门控 / 级联后代 / 深度)。所有函数
 * 与 react-flow 运行时解耦 (只用其 Node/Edge 类型, 编译期擦除), 可在 node 环境单测。
 */

import type { Node, Edge } from "@xyflow/react";
import { inputLabel, INPUT_BBOX_PROMPT_ID, INPUT_CROP_ID } from "@/api/capabilityInputs";
import {
  TASKS,
  MODALITIES,
  type ModalityId,
  type TaskId,
} from "@/api/generated/capabilityVocab.gen";
import type { PipelineSource, PipelineStagePayload } from "@/hooks/usePreannotation";

export interface StageEntry {
  sid: string;
  /** v0.21.5 · 输入节点(编排首节点)parentSid=null; 其余为父 sid。源不再是合成的画布外 root,
   *  而是 stagesGraph 内一条 parentSid=null 的普通 entry。 */
  parentSid: string | null;
}

export type StageRole = {
  label: string;
  variant: "accent" | "ai" | "success";
  icon: "box" | "sparkles" | "tag";
};

/** 输入节点 sid (纯数据源, parentSid 链的根, parentSid=null)。 */
export const ROOT_SID = "root";

/**
 * v0.21.6 · 首模型 stage 的固定 sid (输入节点唯一子, 承接原源检测/tracker 模型配置)。
 * 输入节点=纯数据源(不绑模型), 首模型 stage=后端 stage 0(parent_stage=null)。母计划 frame/video
 * 双分支(多首模型 stage)留 v0.21.7; 本版单首模型 stage 用此固定 sid, cfg 绑它。
 */
export const SOURCE_SID = "source";

/** 受限树形最大深度 (源=1, 下游 2/3)。 */
export const MAX_DEPTH = 3;

/** 阶段是否产几何 (可作下游的父): crop-detect / box-seg 产几何, 分类产属性。 */
export function producesGeometry(payload: PipelineStagePayload | null | undefined): boolean {
  const t = payload?.write?.target ?? "attributes";
  return t === "geometry" || t === "intermediate";
}

/** 角色徽标: crop-detect(input.mode=crop)=检测; 其它产几何=分割; ocr=识别; 否则=分类。 */
export function roleOf(payload: PipelineStagePayload | null | undefined): StageRole {
  if (producesGeometry(payload)) {
    return payload?.input?.mode === INPUT_CROP_ID
      ? { label: "检测", variant: "accent", icon: "box" }
      : { label: "分割", variant: "ai", icon: "sparkles" };
  }
  if (payload?.task === "ocr") {
    return { label: "识别", variant: "success", icon: "tag" };
  }
  return { label: "分类", variant: "success", icon: "tag" };
}

/** 节点详情行: 产几何=命名·模型; 分类=带前缀写回键 / 全部属性。 */
export function detailOf(payload: PipelineStagePayload | null | undefined): string {
  if (!payload) return "未就绪";
  if (producesGeometry(payload)) {
    return (payload.label ? `${payload.label} · ` : "") + (payload.model_id ?? "");
  }
  const keys = payload.write?.keys ?? [];
  const prefix = payload.label ? `${payload.label}_` : "";
  return keys.length > 0 ? keys.map((k) => prefix + k).join(", ") : "全部属性";
}

/** 父框类别过滤摘要: "仅 person, car" / "全部框"。 */
export function classFilterText(payload: PipelineStagePayload | null | undefined): string {
  const cf = payload?.parent_class_filter;
  return cf && cf.length > 0 ? `仅 ${cf.join(", ")}` : "全部框";
}

/** ROI / 投递摘要: "裁剪 · pad 0.08" / "整图框提示" / ""。 */
export function roiText(payload: PipelineStagePayload | null | undefined): string {
  const roi = payload?.roi;
  if (!roi) return "";
  return roi.mode === "crop" ? `裁剪${roi.pad != null ? ` · pad ${roi.pad}` : ""}` : "整图框提示";
}

/** 变体摘要: "sam_variant=large, ..." / ""。 */
export function variantText(payload: PipelineStagePayload | null | undefined): string {
  const v = payload?.model_variants;
  if (!v) return "";
  return Object.entries(v)
    .map(([k, val]) => `${k}=${val}`)
    .join(", ");
}

/** 分类下游 model 所需的最小结构 (MLModelCapability / CapabilityInstanceModel 公共子集)。 */
export interface DownstreamModelLike {
  task?: string;
  supported_prompts?: string[];
  is_interactive?: boolean;
  composition?: string;
}

/**
 * 下游 model 归类旗标 (单一真相; 项目侧 StageCard 与全局侧 GlobalStageInspector 共用)。
 * 三类判据互斥 (task 单值), 语义:
 * - isCropDetectGeometry: 普通检测器在父 crop 上检子物体 (crop 投递 + 坐标回映, 产几何)。
 * - isBoxSegGeometry: box-seg (segmentation + bbox prompt) 消费上游框出 mask (geometry 投递, 产几何)。
 * - isOcrRecognize: rec 原子在父 crop 上认字 (crop 投递, 产 text/orientation/language 属性)。
 * 三者皆非 → 分类下游 (crop 投递, 产属性)。
 */
export interface DownstreamKind {
  isBoxSegGeometry: boolean;
  isCropDetectGeometry: boolean;
  /** 产几何的两类 (box-seg / crop-detect) 之一; 用于隐藏属性字段、允许作父阶段。 */
  isGeometryDownstream: boolean;
  isOcrRecognize: boolean;
}

export function classifyDownstream(model: DownstreamModelLike | null | undefined): DownstreamKind {
  const task = normalizeTask(model?.task ?? "");
  const isBoxSegGeometry =
    task === "segment" &&
    (model?.supported_prompts ?? []).includes("bbox") &&
    !model?.is_interactive;
  const isCropDetectGeometry = task === "detect" && !model?.is_interactive;
  const isOcrRecognize =
    task === "ocr" && model?.composition !== "composite" && !model?.is_interactive;
  return {
    isBoxSegGeometry: !!isBoxSegGeometry,
    isCropDetectGeometry: !!isCropDetectGeometry,
    isGeometryDownstream: !!(isBoxSegGeometry || isCropDetectGeometry),
    isOcrRecognize: !!isOcrRecognize,
  };
}

/** 下游阶段"内生形态": roi/input/write 由 model task 定死 (不给用户手选)。 */
export interface DownstreamShape {
  role: "检测" | "分割" | "识别" | "分类";
  roiMode: "crop" | "geometry";
  /** 仅检测下游显式下发 input=crop; 其余省略, 后端按 supported_inputs 烘焙。 */
  inputMode?: "crop";
  writeTarget: "geometry" | "attributes";
  /** 是否写属性 (决定是否出 write.keys / label 字段)。 */
  isAttributes: boolean;
}

export function deriveDownstreamShape(model: DownstreamModelLike): DownstreamShape {
  const k = classifyDownstream(model);
  if (k.isCropDetectGeometry) {
    return {
      role: "检测",
      roiMode: "crop",
      inputMode: "crop",
      writeTarget: "geometry",
      isAttributes: false,
    };
  }
  if (k.isBoxSegGeometry) {
    return { role: "分割", roiMode: "geometry", writeTarget: "geometry", isAttributes: false };
  }
  if (k.isOcrRecognize) {
    return { role: "识别", roiMode: "crop", writeTarget: "attributes", isAttributes: true };
  }
  return { role: "分类", roiMode: "crop", writeTarget: "attributes", isAttributes: true };
}

/** 源阶段 model 所需最小结构 (MLModelCapability / CapabilityInstanceModel 公共子集)。 */
export interface SourceModelLike {
  task?: string;
  supported_inputs?: string[];
}

/**
 * v0.21.5 · 输入节点"内生形态"。此前 v0.21.1 WS0 的 deriveSourceShape 从 model.supported_inputs
 * 反推源类型, 现退役 —— 源类型 (data_type) / 执行单位 (execution_unit) 改由输入节点
 * `source:{}` 直接携带 (SSOT 前移到 graph)。角色 / 产物仍随 model.task (detection→目标检测 /
 * tracker→视频追踪), 因为输入节点仍绑一个源检测/追踪模型。
 */
export interface SourceNodeShape {
  /** 角色徽标 (走 TASKS[task].label)。 */
  role: StageRole;
  /** 投喂数据模态 (image | video | ...); 由 source.data_type 决定, 兜底 image。 */
  sourceType: ModalityId;
  /** 模态人类可读名 (图像 / 视频)。 */
  sourceTypeLabel: string;
  /** 执行单位人类可读名 (整段序列 / 逐帧 / 场景); 无则 undefined。 */
  executionUnitLabel?: string;
  /** 产物名词 (检测框 / 轨迹); 项目侧输入节点详情行。 */
  productLabel: string;
  /** 源计数标签 (检出 / 轨迹); 画布 footer。 */
  countLabel: string;
}

function isTaskId(task: string | undefined): task is TaskId {
  return task != null && task in TASKS;
}

const EXECUTION_UNIT_LABELS: Record<string, string> = {
  video: "整段序列",
  frame: "逐帧",
  scene: "场景",
};

export function sourceNodeShape(
  source: PipelineSource | null | undefined,
  model: SourceModelLike | null | undefined,
): SourceNodeShape {
  const task = normalizeTask(model?.task ?? "");
  const meta = isTaskId(task) ? TASKS[task] : undefined;
  const sourceType = (source?.data_type as ModalityId | undefined) ?? "image";
  const isTracker = task === "tracker";
  return {
    role: { label: meta?.label ?? "检测", variant: "accent", icon: "box" },
    sourceType,
    sourceTypeLabel: MODALITIES[sourceType]?.label ?? sourceType,
    executionUnitLabel: source?.execution_unit
      ? EXECUTION_UNIT_LABELS[source.execution_unit]
      : undefined,
    productLabel: isTracker ? "轨迹" : "检测框",
    countLabel: isTracker ? "轨迹" : "检出",
  };
}

/** 阶段模型能力旗标 (StageCard 自报, 供画布作可达性 / 产属性警示)。 */
export interface StageCaps {
  /** Names returned by this stage, after native-index filtering and project binding. */
  outputClasses?: string[];
  /** capabilities 查询已就绪 (否则不判, 免误报)。 */
  hasCapabilities: boolean;
  /** 选中 model 自报了 supported_inputs (老 backend 缺省时为 false, 跳过可达性判)。 */
  knownInputs: boolean;
  acceptsCrop: boolean;
  acceptsBboxPrompt: boolean;
  producesAttributes: boolean;
  /**
   * v0.19.2 WS1 · 选中 model 是否产类别属性 (output_attribute_types 含 "class")。
   * undefined = model 未自报属性类型 (无法判, 不警示); false = 自报了但不含 class。
   */
  producesClass?: boolean;
  /**
   * v0.19.3 WS2 · 选中 model 是否可批量 (resource_profile.batchable)。
   * undefined = 未自报 (老 backend, 不警示); false = 自报交互/有状态, 不能进批量预标。
   */
  batchable?: boolean;
}

/**
 * 节点警示文案 (标红, 不硬拦运行 —— 与端点 422 同判据, 仅前移到画布)。
 * - 模型自报 batchable=false (交互/有状态) → 不能进批量预标 (端点 422, WS2)。
 * - 产几何的子既不接 crop 也不接 bbox_prompt → 不可达 (端点会 422)。
 * - 分类子但所选 model 自报属性类型却不含 class → 属性恒空 (端点 422, WS1/WS2)。
 * - 分类子但后端不产属性 → 属性恒空。
 * 返回 null = 无警示。
 */
export function stageWarning(
  payload: PipelineStagePayload | null | undefined,
  caps: StageCaps | null | undefined,
): string | null {
  if (!payload || !caps || !caps.hasCapabilities) return null;
  // batchable 对源/下游均适用, 优先判 (与端点 _assert_capabilities 先判 batchable 对称)。
  if (caps.batchable === false)
    return "该模型为交互/有状态模型（batchable=false），不能用于批量预标流水线";
  if (producesGeometry(payload)) {
    if (caps.knownInputs && !caps.acceptsCrop && !caps.acceptsBboxPrompt)
      return `该模型不接受${inputLabel(INPUT_CROP_ID)}图 / ${inputLabel(
        INPUT_BBOX_PROMPT_ID,
      )}，无法作几何下游（运行将被端点拒绝）`;
    return null;
  }
  // task=ocr 是识别阶段 (产 text/orientation/language 非 class), 不套「分类下游须产 class」判据。
  if (caps.producesClass === false && payload?.task !== "ocr")
    return "该模型不产类别属性（output_attribute_types 不含 class），作分类下游属性恒空";
  if (!caps.producesAttributes) return "该后端不自报输出属性，作下游分类只会重新检测、属性恒空";
  return null;
}

/**
 * 每 sid 深度 (输入节点=0, 首模型 stage=1, 子=父+1)。
 * v0.21.6 · 输入节点是纯数据源不计模型层 → depth 0; MAX_DEPTH=3 因此指模型 stage 的 1..3 三层
 * (输入→检测→子检测→分类)。顺父链递归求值, **与数组顺序无关** —— 改父后子可能排在新父之前,
 * 不能假设父先于子出现 (否则深度被低估 → canAddChild 误判 → 造出超深)。带环兜底。
 */
export function depthBySid(graph: StageEntry[]): Record<string, number> {
  const parentOf: Record<string, string | null> = {};
  for (const e of graph) parentOf[e.sid] = e.parentSid;
  const memo: Record<string, number> = {};
  const depthOf = (sid: string, seen: Set<string>): number => {
    if (memo[sid] != null) return memo[sid];
    const p = parentOf[sid];
    if (p == null || seen.has(sid)) return (memo[sid] = 0); // 输入节点 / 孤儿 / 环 → 0
    seen.add(sid);
    return (memo[sid] = depthOf(p, seen) + 1);
  };
  for (const e of graph) depthOf(e.sid, new Set());
  return memo;
}

/** sid 的全部后代 (不含自身)。用于级联删 + 环检测。 */
export function descendantsOf(graph: StageEntry[], sid: string): Set<string> {
  const dead = new Set<string>();
  const queue = [sid];
  while (queue.length > 0) {
    const cur = queue.shift() as string;
    for (const e of graph) {
      if (e.parentSid === cur && !dead.has(e.sid)) {
        dead.add(e.sid);
        queue.push(e.sid);
      }
    }
  }
  return dead;
}

/** 子树高度 (叶=1, 含 sid 自身那一层): 1 + max(子高度)。 */
export function subtreeHeight(graph: StageEntry[], sid: string): number {
  const kids = graph.filter((e) => e.parentSid === sid);
  if (kids.length === 0) return 1;
  return 1 + Math.max(...kids.map((e) => subtreeHeight(graph, e.sid)));
}

/**
 * sid 是否产几何 (可作下游的父)。
 * - 输入节点(parentSid=null): 纯数据源, 恒 true (其源模型子可挂)。
 * - 源式 stage(父=输入节点): 源检测/tracker 模型, 恒产几何 (payload 由 cfg 管、不在复合器,
 *   故不能靠 producesGeometry(payload); 结构性判为 true, 与 v0.21.5「源恒产几何」一致)。
 * - 其余下游 stage: 按 producesGeometry(payload)。
 */
function sidProducesGeometry(
  graph: StageEntry[],
  payloadBySid: Record<string, PipelineStagePayload | null>,
  sid: string,
): boolean {
  const e = graph.find((x) => x.sid === sid);
  if (!e) return producesGeometry(payloadBySid[sid]);
  if (e.parentSid == null) return true;
  const parent = graph.find((x) => x.sid === e.parentSid);
  if (parent && parent.parentSid == null) return true;
  return producesGeometry(payloadBySid[sid]);
}

/** 能否在 parentSid 下加子: 父产几何 + 深度 < MAX_DEPTH (加完子深度 ≤ MAX_DEPTH)。 */
export function canAddChild(
  graph: StageEntry[],
  payloadBySid: Record<string, PipelineStagePayload | null>,
  parentSid: string,
): boolean {
  if (!sidProducesGeometry(graph, payloadBySid, parentSid)) return false;
  return (depthBySid(graph)[parentSid] ?? 1) < MAX_DEPTH;
}

export interface ReparentCheck {
  ok: boolean;
  reason?: string;
}

/**
 * 把 childSid 改挂到 newParentSid 是否合法 (把 0.18.15 受限树形 gating 反向约束进 DAG):
 * 1. 不自连 / 不连回原父 (无意义)。
 * 2. 无环: newParent 不能是 child 的后代。
 * 3. 父产几何 (源恒产几何)。
 * 4. 改父后整子树深度 ≤ MAX_DEPTH。
 * 单父由树模型天然保证 (改父=替换入边)。
 * 子侧 supported_inputs 可达性不在此判 —— 由端点 422 兜底 (与 0.18.15 一致)。
 */
export function canReparent(
  graph: StageEntry[],
  payloadBySid: Record<string, PipelineStagePayload | null>,
  childSid: string,
  newParentSid: string,
): ReparentCheck {
  const cur = graph.find((e) => e.sid === childSid);
  if (cur && cur.parentSid == null) return { ok: false, reason: "输入节点不可改父" };
  if (newParentSid === childSid) return { ok: false, reason: "不能连到自身" };
  if (cur && cur.parentSid === newParentSid) return { ok: false, reason: "已是当前父阶段" };
  if (descendantsOf(graph, childSid).has(newParentSid))
    return { ok: false, reason: "不能连到自己的后代（成环）" };
  if (!sidProducesGeometry(graph, payloadBySid, newParentSid))
    return { ok: false, reason: "父阶段须产几何（检测/分割）" };
  const newChildDepth = (depthBySid(graph)[newParentSid] ?? 1) + 1;
  if (newChildDepth + subtreeHeight(graph, childSid) - 1 > MAX_DEPTH)
    return { ok: false, reason: `改父后将超过最大 ${MAX_DEPTH} 层` };
  return { ok: true };
}

/** 应用改父: 返回新 stagesGraph (childSid.parentSid = newParentSid), 校验交给调用方。 */
export function reparent(
  graph: StageEntry[],
  childSid: string,
  newParentSid: string,
): StageEntry[] {
  return graph.map((e) => (e.sid === childSid ? { ...e, parentSid: newParentSid } : e));
}

// ── 布局 + react-flow 派生 ─────────────────────────────────────────────

/** buildFlow 的输入: 每节点的展示模型。输入节点 = parentSid==null 的普通 entry (不再 kind 二分)。 */
export interface GraphNodeModel {
  sid: string;
  parentSid: string | null;
  role: StageRole;
  detail: string;
  runState: "pending" | "running" | "done";
  /** 迷你计数 (下游: 目标/成功; 源: detected 走 ok 槽)。 */
  targeted?: number;
  ok?: number;
  producesGeometry: boolean;
  canAddChild: boolean;
  conflict: boolean;
  // v0.21.1 WS0 · 输入节点「源类型」徽标 (图像 / 视频) + 计数标签 (检出 / 轨迹); 下游为 undefined。
  sourceTypeLabel?: string;
  sourceCountLabel?: string;
  // v0.21.5 · 输入节点「执行单位」徽标 (整段序列 / 逐帧); 下游为 undefined。
  executionUnitLabel?: string;
  // ── v0.18.16 §13 信息增强 ──
  /** backend 名 (副标题); 看不出用哪个后端是主要痛点。 */
  backendName?: string;
  /** 已配置就绪 (payload != null); 否则节点显「待配置」虚线。 */
  ready: boolean;
  /** 警示文案 (标红 + tooltip), null=无。 */
  warning?: string | null;
  /** 父框过滤摘要 (节点芯片): "仅 person" / "全部框"。 */
  classFilter?: string;
  // 以下仅 hover 浮层显示:
  modelId?: string;
  taskType?: string;
  roiInfo?: string;
  variantInfo?: string;
}

export interface StageNodeData extends GraphNodeModel {
  selected: boolean;
  [key: string]: unknown;
}

const COL_W = 230;
const ROW_H = 116;

/**
 * 分层布局 + react-flow 派生: col = depth-1, 同列内按 DFS 出现序排行。
 * nodes/edges 为受控派生 (位置由算法定, nodesDraggable=false 防漂移)。
 */
export function buildFlow(
  models: GraphNodeModel[],
  selectedSid: string | null,
): { nodes: Node<StageNodeData>[]; edges: Edge[] } {
  const bySid = new Map(models.map((m) => [m.sid, m]));
  const depth: Record<string, number> = {};
  // seen 防护: 即便上游漏过了环 / 超深图, 也不让递归打爆栈 (画布崩溃)。
  const computeDepth = (sid: string, seen: Set<string>): number => {
    if (depth[sid] != null) return depth[sid];
    const m = bySid.get(sid);
    const p = m?.parentSid;
    if (p == null || seen.has(sid)) return (depth[sid] = 1);
    seen.add(sid);
    return (depth[sid] = computeDepth(p, seen) + 1);
  };
  models.forEach((m) => computeDepth(m.sid, new Set()));

  // DFS 从源出发, 给每列分配递增 row, 保证兄弟稳定纵向排布。
  const rowInCol: Record<number, number> = {};
  const yBySid: Record<string, number> = {};
  const childrenOf = (sid: string | null) => models.filter((m) => m.parentSid === sid);
  const visit = (m: GraphNodeModel) => {
    const col = depth[m.sid] - 1;
    rowInCol[col] = rowInCol[col] ?? 0;
    yBySid[m.sid] = rowInCol[col] * ROW_H;
    rowInCol[col] += 1;
    childrenOf(m.sid).forEach(visit);
  };
  const roots = models.filter((m) => m.parentSid == null);
  roots.forEach(visit);

  const nodes: Node<StageNodeData>[] = models.map((m) => ({
    id: m.sid,
    // v0.21.5 · 单一 node type; 输入 handle 由 parentSid==null 在节点组件内决定 (去 source/stage 二分)。
    type: "stage",
    position: { x: (depth[m.sid] - 1) * COL_W, y: yBySid[m.sid] ?? 0 },
    data: { ...m, selected: m.sid === selectedSid },
    selected: m.sid === selectedSid,
    draggable: false,
  }));

  const edges: Edge[] = models
    .filter((m) => m.parentSid != null)
    .map((m) => ({
      id: `${m.parentSid}->${m.sid}`,
      source: m.parentSid as string,
      target: m.sid,
      type: "smoothstep",
      reconnectable: true,
    }));

  return { nodes, edges };
}
