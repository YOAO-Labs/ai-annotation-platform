import { normalizeTask } from "@/api/generated/capabilityVocab.gen";

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
  parentSid: string | null;
}

export type StageRole = {
  label: string;
  variant: "accent" | "ai" | "success";
  icon: "box" | "sparkles" | "tag";
};

export const ROOT_SID = "root";

export const SOURCE_SID = "source";

export const MAX_DEPTH = 3;

export function producesGeometry(payload: PipelineStagePayload | null | undefined): boolean {
  const t = payload?.write?.target ?? "attributes";
  return t === "geometry" || t === "intermediate";
}

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

export function detailOf(payload: PipelineStagePayload | null | undefined): string {
  if (!payload) return "未就绪";
  if (producesGeometry(payload)) {
    return (payload.label ? `${payload.label} · ` : "") + (payload.model_id ?? "");
  }
  const keys = payload.write?.keys ?? [];
  const prefix = payload.label ? `${payload.label}_` : "";
  return keys.length > 0 ? keys.map((k) => prefix + k).join(", ") : "全部属性";
}

export function classFilterText(payload: PipelineStagePayload | null | undefined): string {
  const cf = payload?.parent_class_filter;
  return cf && cf.length > 0 ? `仅 ${cf.join(", ")}` : "全部框";
}

export function roiText(payload: PipelineStagePayload | null | undefined): string {
  const roi = payload?.roi;
  if (!roi) return "";
  return roi.mode === "crop" ? `裁剪${roi.pad != null ? ` · pad ${roi.pad}` : ""}` : "整图框提示";
}

export function variantText(payload: PipelineStagePayload | null | undefined): string {
  const v = payload?.model_variants;
  if (!v) return "";
  return Object.entries(v)
    .map(([k, val]) => `${k}=${val}`)
    .join(", ");
}

export interface DownstreamModelLike {
  task?: string;
  supported_prompts?: string[];
  is_interactive?: boolean;
  composition?: string;
}

export interface DownstreamKind {
  isBoxSegGeometry: boolean;
  isCropDetectGeometry: boolean;
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

export interface DownstreamShape {
  role: "检测" | "分割" | "识别" | "分类";
  roiMode: "crop" | "geometry";
  inputMode?: "crop";
  writeTarget: "geometry" | "attributes";
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

export interface SourceModelLike {
  task?: string;
  supported_inputs?: string[];
}

export interface SourceNodeShape {
  role: StageRole;
  sourceType: ModalityId;
  sourceTypeLabel: string;
  executionUnitLabel?: string;
  productLabel: string;
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

export interface StageCaps {
  outputClasses?: string[];
  hasCapabilities: boolean;
  knownInputs: boolean;
  acceptsCrop: boolean;
  acceptsBboxPrompt: boolean;
  producesAttributes: boolean;
  producesClass?: boolean;
  batchable?: boolean;
}

export function stageWarning(
  payload: PipelineStagePayload | null | undefined,
  caps: StageCaps | null | undefined,
): string | null {
  if (!payload || !caps || !caps.hasCapabilities) return null;
  if (caps.batchable === false)
    return "该模型为交互/有状态模型（batchable=false），不能用于批量预标流水线";
  if (producesGeometry(payload)) {
    if (caps.knownInputs && !caps.acceptsCrop && !caps.acceptsBboxPrompt)
      return `该模型不接受${inputLabel(INPUT_CROP_ID)}图 / ${inputLabel(
        INPUT_BBOX_PROMPT_ID,
      )}，无法作几何下游（运行将被端点拒绝）`;
    return null;
  }
  if (caps.producesClass === false && payload?.task !== "ocr")
    return "该模型不产类别属性（output_attribute_types 不含 class），作分类下游属性恒空";
  if (!caps.producesAttributes) return "该后端不自报输出属性，作下游分类只会重新检测、属性恒空";
  return null;
}

export function depthBySid(graph: StageEntry[]): Record<string, number> {
  const parentOf: Record<string, string | null> = {};
  for (const e of graph) parentOf[e.sid] = e.parentSid;
  const memo: Record<string, number> = {};
  const depthOf = (sid: string, seen: Set<string>): number => {
    if (memo[sid] != null) return memo[sid];
    const p = parentOf[sid];
    if (p == null || seen.has(sid)) return (memo[sid] = 0);
    seen.add(sid);
    return (memo[sid] = depthOf(p, seen) + 1);
  };
  for (const e of graph) depthOf(e.sid, new Set());
  return memo;
}

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

export function subtreeHeight(graph: StageEntry[], sid: string): number {
  const kids = graph.filter((e) => e.parentSid === sid);
  if (kids.length === 0) return 1;
  return 1 + Math.max(...kids.map((e) => subtreeHeight(graph, e.sid)));
}

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

export function reparent(
  graph: StageEntry[],
  childSid: string,
  newParentSid: string,
): StageEntry[] {
  return graph.map((e) => (e.sid === childSid ? { ...e, parentSid: newParentSid } : e));
}

export interface GraphNodeModel {
  sid: string;
  parentSid: string | null;
  role: StageRole;
  detail: string;
  runState: "pending" | "running" | "done";
  targeted?: number;
  ok?: number;
  producesGeometry: boolean;
  canAddChild: boolean;
  conflict: boolean;
  sourceTypeLabel?: string;
  sourceCountLabel?: string;
  executionUnitLabel?: string;
  backendName?: string;
  ready: boolean;
  warning?: string | null;
  classFilter?: string;
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

export function buildFlow(
  models: GraphNodeModel[],
  selectedSid: string | null,
): { nodes: Node<StageNodeData>[]; edges: Edge[] } {
  const bySid = new Map(models.map((m) => [m.sid, m]));
  const depth: Record<string, number> = {};
  const computeDepth = (sid: string, seen: Set<string>): number => {
    if (depth[sid] != null) return depth[sid];
    const m = bySid.get(sid);
    const p = m?.parentSid;
    if (p == null || seen.has(sid)) return (depth[sid] = 1);
    seen.add(sid);
    return (depth[sid] = computeDepth(p, seen) + 1);
  };
  models.forEach((m) => computeDepth(m.sid, new Set()));

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
